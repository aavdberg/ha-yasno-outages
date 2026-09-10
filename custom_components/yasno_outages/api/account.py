"""
Authenticated account/customer data API for Yasno outages.

Requires a valid access token obtained via `YasnoAuthApi`. Unlike the
public `blackout-service` endpoints, these are tied to the logged-in
user's YASNO account: the account's own address/group is resolved
server-side, no manually configured region/DSO/group is required.
"""

from __future__ import annotations

import datetime
import logging

import aiohttp

from .const import (
    ACCOUNT_BOOK_AVAILABLE_YEARS_ENDPOINT,
    ACCOUNT_BOOK_HISTORY_ENDPOINT,
    ACCOUNTS_ENDPOINT,
    AUTH_ADDRESSES_ENDPOINT,
    AUTH_CURRENT_SLOTS_ENDPOINT,
    CONTRACTS_ENDPOINT,
    DEBT_ENDPOINT,
    LAST_FEES_ENDPOINT,
    RECOMMENDED_AMOUNT_ENDPOINT,
    TARIFF_ENDPOINT,
)
from .models import (
    YasnoAccount,
    YasnoAccountBookMonth,
    YasnoAccountDebt,
    YasnoAuthAddress,
    YasnoConsumption,
    YasnoContract,
    YasnoMeterReading,
    YasnoRecommendedAmount,
    YasnoTariff,
    YasnoTariffPeriod,
    YasnoTariffPrice,
    YasnoTariffTier,
)

LOGGER = logging.getLogger(__name__)


class AccountApi:
    """API for fetching authenticated customer account data."""

    def __init__(self, access_token: str) -> None:
        """Initialize the AccountApi with a bearer access token."""
        self.access_token = access_token

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: dict | None = None,
    ) -> dict | list | None:
        """Fetch and parse JSON from an authenticated endpoint."""
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            async with session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError:
            LOGGER.exception("Error fetching data from %s", url)
            return None

    async def fetch_accounts(self) -> list[YasnoAccount]:
        """Fetch the logged-in user's accounts (address, tariff, supplier)."""
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(session, ACCOUNTS_ENDPOINT)
        if not data:
            return []
        return [self._parse_account(item) for item in data]

    async def fetch_contracts(self) -> list[YasnoContract]:
        """Fetch the logged-in user's contracts (includes current balance)."""
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(session, CONTRACTS_ENDPOINT)
        if not data:
            return []
        return [self._parse_contract(item) for item in data]

    async def fetch_debt(self, account_ids: list[int]) -> dict[int, YasnoAccountDebt]:
        """Fetch balance/last meter readings for the given account ids."""
        if not account_ids:
            return {}
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(
                session,
                DEBT_ENDPOINT,
                params={"accountIds": ",".join(str(i) for i in account_ids)},
            )
        if not data:
            return {}
        return {
            int(account_id): self._parse_debt(item) for account_id, item in data.items()
        }

    async def fetch_addresses(self) -> list[YasnoAuthAddress]:
        """
        Fetch the address(es)/region/DSO/group linked to the account.

        This can replace the manual region -> DSO -> group config flow
        selection once the user is logged in.
        """
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(session, AUTH_ADDRESSES_ENDPOINT)
        if not data:
            return []
        return [self._parse_address(item) for item in data]

    async def fetch_current_slots(self) -> list[dict]:
        """Fetch the current outage status for the account's address(es)."""
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(session, AUTH_CURRENT_SLOTS_ENDPOINT)
        return data or []

    async def fetch_tariff(self, account_id: int) -> YasnoTariff | None:
        """Fetch the tariff plan (day/night prices, DSO fees) for an account."""
        url = TARIFF_ENDPOINT.format(account_id=account_id)
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(session, url)
        if not data:
            return None
        return self._parse_tariff(data)

    async def fetch_recommended_amount(
        self,
        account_id: int,
    ) -> YasnoRecommendedAmount | None:
        """Fetch the recommended top-up amount (balance, next accrual)."""
        url = RECOMMENDED_AMOUNT_ENDPOINT.format(account_id=account_id)
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(session, url)
        if not data:
            return None
        return self._parse_recommended_amount(data)

    async def fetch_account_book_history(
        self,
        account_id: int,
        limit: int = 3,
    ) -> list[YasnoAccountBookMonth]:
        """Fetch the most recent months of consumption/billing history."""
        url = ACCOUNT_BOOK_HISTORY_ENDPOINT.format(account_id=account_id)
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(
                session,
                url,
                params={"offset": 0, "limit": limit},
            )
        if not data:
            return []
        months = [
            month for year in data.get("items", []) for month in year.get("months", [])
        ]
        return [self._parse_account_book_month(month) for month in months]

    async def fetch_account_book_available_years(self, account_id: int) -> list[int]:
        """Fetch the list of years for which account book history is available."""
        url = ACCOUNT_BOOK_AVAILABLE_YEARS_ENDPOINT.format(account_id=account_id)
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(session, url)
        if not data:
            return []
        return list(data.get("years", []))

    async def fetch_last_fees(self) -> dict:
        """
        Fetch the last fees entry.

        As observed in captured traffic, this endpoint currently always
        returns an empty object regardless of account state; kept for API
        completeness / future use.
        """
        async with aiohttp.ClientSession() as session:
            data = await self._get_json(session, LAST_FEES_ENDPOINT)
        return data or {}

    @staticmethod
    def _parse_account(item: dict) -> YasnoAccount:
        """Parse a raw accounts entry into a YasnoAccount."""
        details = item.get("details", {}).get("b2C", {})
        return YasnoAccount(
            id=item["id"],
            account_number=item.get("accountNumber", ""),
            account_name=item.get("accountName", ""),
            customer_type=item.get("customerType", ""),
            address=details.get("address", ""),
            supplier=item.get("supplier", ""),
            region=item.get("region", ""),
            billing_status=item.get("billingStatus", ""),
        )

    @staticmethod
    def _parse_contract(item: dict) -> YasnoContract:
        """Parse a raw contracts entry into a YasnoContract."""
        return YasnoContract(
            id=item["id"],
            name=item.get("name", ""),
            application_type=item.get("applicationType", ""),
            debt=float(item.get("debt", 0)),
            account_number=item.get("accountNumber", ""),
            address=item.get("address", ""),
            region=item.get("region", ""),
            supplier=item.get("supplier", ""),
            billing_status=item.get("billingStatus", ""),
            can_be_paid=bool(item.get("canBePaid", False)),
        )

    @staticmethod
    def _parse_debt(item: dict) -> YasnoAccountDebt:
        """Parse a raw debt entry into a YasnoAccountDebt."""
        reading = None
        readings = item.get("lastMeterReadings")
        if readings:
            zones = {
                zone["zone"]: zone["value"]
                for zone in readings.get("meteringReadings", [])
            }
            created_on_raw = readings.get("createdOn")
            reading = YasnoMeterReading(
                created_on=datetime.datetime.fromisoformat(created_on_raw)
                if created_on_raw
                else None,
                owner=readings.get("owner", ""),
                reading_method=readings.get("readingMethod", ""),
                day_value=zones.get("Day"),
                night_value=zones.get("Night"),
            )
        return YasnoAccountDebt(
            balance=float(item.get("balance", 0)),
            electric_meter_exists=bool(item.get("electricMeterExists", False)),
            has_invoice_file=bool(item.get("hasInvoiceFile", False)),
            is_paper_bill_required=bool(item.get("isPaperBillRequired", False)),
            last_meter_reading=reading,
        )

    @staticmethod
    def _parse_address(item: dict) -> YasnoAuthAddress:
        """Parse a raw authenticated address entry into a YasnoAuthAddress."""
        dso = item.get("dso", {})
        return YasnoAuthAddress(
            address_id=item["addressId"],
            name=item.get("name", ""),
            region_id=item.get("regionId", 0),
            group=item.get("group", 0),
            subgroup=item.get("subgroup", 0),
            dso_id=dso.get("id", 0),
            dso_name=dso.get("name", ""),
        )

    @staticmethod
    def _parse_tariff(item: dict) -> YasnoTariff:
        """Parse a raw tariff entry into a YasnoTariff."""
        overview = item.get("overview", {})
        dso = item.get("distributionSystemOperator", {})
        distribution = dso.get("distribution") or {}
        transfer = dso.get("transfer") or {}

        periods = []
        for period in item.get("priceAtPeriods", []):
            tiers = []
            for tier in period.get("tiers", []):
                prices = tuple(
                    YasnoTariffPrice(zone=price["zone"], price=float(price["price"]))
                    for price in tier.get("tariff", [])
                )
                tiers.append(
                    YasnoTariffTier(
                        name=tier.get("name"),
                        prices=prices,
                        for_all_volume=bool(tier.get("forAllVolume", False)),
                    ),
                )
            periods.append(
                YasnoTariffPeriod(
                    start_date=datetime.datetime.fromisoformat(period["startDate"]),
                    end_date=datetime.datetime.fromisoformat(period["endDate"]),
                    tiers=tuple(tiers),
                ),
            )

        return YasnoTariff(
            name=overview.get("name", ""),
            dso_name=dso.get("name", ""),
            distribution_price=(
                float(distribution["value"]) if "value" in distribution else None
            ),
            transfer_price=(float(transfer["value"]) if "value" in transfer else None),
            periods=tuple(periods),
        )

    @staticmethod
    def _parse_recommended_amount(item: dict) -> YasnoRecommendedAmount:
        """Parse a raw recommended-amount entry into a YasnoRecommendedAmount."""
        next_accrual = item.get("nextAccrual") or {}
        start_raw = next_accrual.get("start")
        end_raw = next_accrual.get("end")
        recommended_amount = item.get("recommendedAmount")
        return YasnoRecommendedAmount(
            balance=float(item.get("balance", 0)),
            recommended_amount=(
                float(recommended_amount) if recommended_amount is not None else None
            ),
            recommended_amounts=tuple(
                float(amount) for amount in item.get("recommendedAmounts", [])
            ),
            next_accrual_start=(
                datetime.datetime.fromisoformat(start_raw) if start_raw else None
            ),
            next_accrual_end=(
                datetime.datetime.fromisoformat(end_raw) if end_raw else None
            ),
        )

    @staticmethod
    def _parse_account_book_month(item: dict) -> YasnoAccountBookMonth:
        """Parse a raw account-book month entry into a YasnoAccountBookMonth."""
        consumptions = tuple(
            YasnoConsumption(zone=entry["zone"], value=float(entry["value"]))
            for entry in item.get("consumptions", [])
        )
        # Past months use "balanceEndOfMonth"; the current (in-progress) month
        # only has "balance" since it hasn't ended yet.
        balance_end_of_month = item.get("balanceEndOfMonth", item.get("balance"))
        return YasnoAccountBookMonth(
            date=datetime.datetime.fromisoformat(item["date"]),
            consumptions=consumptions,
            charged=float(item.get("charged", 0)),
            paid=float(item.get("paid", 0)),
            balance_end_of_month=(
                float(balance_end_of_month)
                if balance_end_of_month is not None
                else None
            ),
        )
