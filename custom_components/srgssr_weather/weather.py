import asyncio
import base64
import logging
import random
import time
from datetime import datetime
from typing import Iterable, List, Mapping, MutableMapping, Optional

from homeassistant.components.weather import WeatherEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    HTTP_OK,
    TEMP_CELSIUS,
    STATE_UNAVAILABLE,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import HomeAssistantType

from .const import (
    ATTR_API_KEY,
    ATTR_EXPIRES_AT,
    CONF_CONSUMER_KEY,
    CONF_CONSUMER_SECRET,
)

logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistantType, config_entry: ConfigEntry, async_add_entities
) -> None:
    async_add_entities((SRGSSTWeather(config_entry.data),))


API_URL = "https://api.srgssr.ch"

URL_OAUTH = API_URL + "/oauth/v1/accesstoken"

URL_FORECASTS = API_URL + "/srf-meteo/forecast/{geolocationId}"


def _check_client_credentials_response(d: dict) -> None:
    EXPECTED_KEYS = {"issued_at", "expires_in", "access_token"}

    if "issued_at" not in d:
        d["issued_at"] = int(time.time())

    missing = EXPECTED_KEYS - d.keys()
    if missing:
        logger.warning(
            f"received client credentials response with missing keys: {missing} ({d})"
        )
        raise ValueError("client credentials response missing keys", missing)


async def request_access_token(hass: HomeAssistantType, key: str, secret: str) -> dict:
    session = async_get_clientsession(hass)

    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    params = {"grant_type": "client_credentials"}
    async with session.post(URL_OAUTH, params=params, headers=headers) as resp:
        data = await resp.json()

    _check_client_credentials_response(data)
    return data


async def _renew_api_key(hass: HomeAssistantType, data: MutableMapping) -> None:
    token_data = await request_access_token(
        hass, data[CONF_CONSUMER_KEY], data[CONF_CONSUMER_SECRET]
    )
    logger.debug("token data: %s", token_data)

    try:
        data[ATTR_EXPIRES_AT] = (
            int(token_data["expires_in"]) + int(token_data["issued_at"]) // 1000
        )
        data[ATTR_API_KEY] = token_data["access_token"]
    except Exception:
        logger.exception(
            "exception while parsing access token response: %s", token_data
        )
        raise


async def get_api_key(hass: HomeAssistantType, data: MutableMapping) -> str:
    try:
        expires_at = data[ATTR_EXPIRES_AT]
    except KeyError:
        renew = True
    else:
        renew = time.time() >= expires_at

    if renew:
        logger.info("renewing api key")
        await _renew_api_key(hass, data)

    return data[ATTR_API_KEY]


class SRGSSTWeather(WeatherEntity):
    def __init__(self, config: dict) -> None:
        self._config = config
        self._default_params = {
            "latitude": str(config[CONF_LATITUDE]),
            "longitude": str(config[CONF_LONGITUDE]),
        }
        self._api_data = dict(self._config)
        self.__update_loop_task = None

        self._forecast = []
        self._hourly_forecast = []
        self._state = None
        self._temperature = None
        self._wind_speed = None
        self._wind_bearing = None

        self._state_attrs = {}

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def unique_id(self):
        return f"{self._config[CONF_LATITUDE]}-{self._config[CONF_LONGITUDE]}"

    @property
    def name(self) -> Optional[str]:
        return self._config.get(CONF_NAME)

    @property
    def device_state_attributes(self) -> dict:
        return self._state_attrs

    @property
    def state(self) -> Optional[str]:
        return self._state

    @property
    def temperature(self) -> Optional[float]:
        return self._temperature

    @property
    def temperature_unit(self):
        return TEMP_CELSIUS

    @property
    def pressure(self) -> Optional[float]:
        return None

    @property
    def humidity(self) -> Optional[float]:
        return None

    @property
    def visibility(self) -> Optional[float]:
        return None

    @property
    def wind_speed(self) -> Optional[float]:
        return self._wind_speed

    @property
    def wind_bearing(self) -> Optional[str]:
        return self._wind_bearing

    @property
    def forecast(self) -> List[dict]:
        return self._forecast

    @property
    def hourly_forecast(self) -> List[dict]:
        return self._hourly_forecast

    @property
    def attribution(self) -> str:
        return "SRF Schweizer Radio und Fernsehen"

    async def __get(self, url: str, **kwargs) -> dict:
        session = async_get_clientsession(self.hass)
        api_key = await get_api_key(self.hass, self._api_data)
        weak_update(
            kwargs,
            "headers",
            {
                "Authorization": f"Bearer {api_key}",
            },
        )
        # weak_update(kwargs, "params", self._default_params)
        logger.debug("GET %s with %s", url, kwargs)
        async with session.get(url, **kwargs) as resp:
            if resp.status == HTTP_OK:
                logger.debug(
                    "Rate-limit available %s, rate-limit reset will be on %s",
                    resp.headers.get("x-ratelimit-available"),
                    datetime.fromtimestamp(
                        int(resp.headers.get("x-ratelimit-reset-time", 0)) / 1000
                    ),
                )
            data = await resp.json()
            logger.debug("response: %s", data)
            resp.raise_for_status()

        return data

    async def __update(self) -> None:
        geoid = "{},{}".format(
            self._default_params["latitude"], self._default_params["longitude"]
        )
        url = URL_FORECASTS.format(geolocationId="47.0274,8.3020")
        logger.debug("Updating using URL %s", url)
        data = await self.__get(url)

        logger.debug(data)
        hourlyforecast = data["forecast"]["60minutes"]

        now = datetime.now().astimezone()
        futureforecast = (
            f
            for f in hourlyforecast
            if datetime.fromisoformat(f["local_date_time"]) > now
        )
        forecastnow = next(futureforecast, None)
        if forecastnow is None:
            logger.warning("No forecast found for current hour {}".format(now))
            forecastnow = hourlyforecast[-1]

        logger.debug(forecastnow)

        symbol_id = int(forecastnow["SYMBOL_CODE"])
        self._state = get_condition_from_symbol(symbol_id)
        self._temperature = float(forecastnow["TTT_C"])
        self._wind_speed = float(forecastnow["FF_KMH"])
        self._wind_speed = float(forecastnow["FX_KMH"])
        wind_bearing_deg = float(forecastnow["DD_DEG"])
        self._wind_bearing = deg_to_cardinal(wind_bearing_deg)

        self._state_attrs.update(
            wind_direction=wind_bearing_deg,
            symbol_id=symbol_id,
            precipitation=float(forecastnow["RRR_MM"]),
            rain_probability=float(forecastnow["PROBPCP_PERCENT"]),
        )

        forecast = []
        for raw_day in data["forecast"]["day"]:
            try:
                day = parse_forecast(raw_day)
            except Exception:
                logger.warning(f"failed to parse forecast day: {raw_day}")
                continue
            forecast.append(day)

        self._forecast = forecast

        hourly_forecast = []
        for raw_hour in data["forecast"]["60minutes"]:
            try:
                hour = parse_forecast(raw_hour)
            except Exception:
                logger.warning(f"failed to parse forecast day: {raw_hour}")
                continue
            forecast.append(hour)
        self._hourly_forecast = hourly_forecast

    async def __update_loop(self) -> None:
        while True:
            try:
                await self.__update()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to update weather")
            else:
                await self.async_update_ha_state()

            delay = random.randrange(55, 65) * 60
            await asyncio.sleep(delay)

    async def async_added_to_hass(self) -> None:
        self.__update_loop_task = asyncio.create_task(self.__update_loop())

    async def async_will_remove_from_hass(self) -> None:
        if self.__update_loop_task:
            self.__update_loop_task.cancel()
            self.__update_loop_task = None


def parse_forecast(day: dict) -> dict:
    date = datetime.fromisoformat(day["local_date_time"])

    temp_high = float(day["TX_C"])
    temp_low = float(day["TN_C"])
    symbol_id = int(day["SYMBOL_CODE"])
    condition = get_condition_from_symbol(symbol_id)
    precip_total = float(day["RRR_MM"])
    wind_bearing = int(day["DD_DEG"])
    wind_speed = float(day["FF_KMH"])

    return {
        "datetime": date.isoformat(),
        "temperature": temp_high,
        "condition": condition,
        "symbol_id": symbol_id,
        "templow": temp_low,
        "precipitation": precip_total,
        "wind_bearing": precip_total,
        "wind_speed": wind_speed,
    }


CARDINALS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)

DEG_HALF_CIRCLE = 180
DEG_FULL_CIRCLE = 2 * DEG_HALF_CIRCLE
_CARDINAL_DEGREE = DEG_FULL_CIRCLE / len(CARDINALS)


def deg_to_cardinal(deg: float) -> str:
    i = round((deg % DEG_FULL_CIRCLE) / _CARDINAL_DEGREE)
    return CARDINALS[i % len(CARDINALS)]


# maps the symbol reported by the API to the Material Design icon names used by Home Assistant.
# Sadly this isn't bijective because the API reports lots of weirdly specific states.
# The comments contain the description of each symbol id as reported by SRG SSR.
SYMBOL_STATE_MAP = {
    1: "sunny",  # sonnig
    2: "fog",  # Nebelbänke
    3: "partlycloudy",  # teils sonnig
    4: "rainy",  # Regenschauer
    5: "lightning-rainy",  # Regenegenschauer mit Gewitter
    6: "snowy",  # Schneeschauer
    7: "snowy-rainy",  # sonnige Abschnitte und einige Gewitter mit Schnee (undocumented)
    8: "snowy-rainy",  # Schneeregenschauer
    9: "snowy-rainy",  # wechselhaft mit Schneeregenschauern und Gewittern (undocumented)
    10: "sunny",  # ziemlich sonnig
    11: "sunny",  # sonnig, aber auch einzelne Schauer (undocumented)
    12: "sunny",  # sonnig und nur einzelne Gewitter (undocumented)
    13: "sunny",  # sonnig und nur einzelne Schneeschauer (undocumented)
    14: "sunny",  # sonnig, einzelne Schneeschauer, dazwischen sogar Blitz und Donner (undocumented)
    15: "sunny",  # sonnig und nur einzelne Schauer, vereinzelt auch Flocken (undocumented)
    16: "sunny",  # oft sonnig, nur einzelne gewittrige Schauer, teils auch Flocken (undocumented)
    17: "fog",  # Nebel
    18: "cloudy",  # stark bewölkt (undocumented)
    19: "cloudy",  # bedeckt
    20: "rainy",  # regnerisch
    21: "snowy",  # Schneefall
    22: "snowy-rainy",  # Schneeregen
    23: "pouring",  # Dauerregen (undocumented)
    24: "snowy",  # starker Schneefall (undocumented)
    25: "rainy",  # Regenschauer26: "lightning",  # stark bewölkt und einige Gewitter
    26: "lightning",  # stark bewölkt und einige Gewitter (undocumented)
    27: "snowy",  # trüb mit einigen Schneeschauern (undocumented)
    28: "cloudy",  # stark bewölkt, Schneeschauer, dazwischen Blitz und Donner (undocumented)
    29: "snowy-rainy",  # ab und zu Schneeregen (undocumented)
    30: "snowy-rainy",  # Schneeregen, einzelne Gewitter (undocumented)
    -1: "clear-night",  # klar
    -2: "fog",  # Nebelbänke
    -3: "cloudy",  # Wolken: Sandsturm
    -4: "rainy",  # Regenschauer
    -5: "lightning-rainy",  # Regenschauer mit Gewitter
    -6: "snowy",  # Schneeschauer
    -7: "snowy",  # einige Gewitter mit Schnee (undocumented)
    -8: "snowy-rainy",  # Schneeregenschauer
    -9: "lightning-rainy",  # wechselhaft mit Schneeregenschauern und Gewittern (undocumented)
    -10: "partlycloudy",  # klare Abschnitte
    -11: "rainy",  # einzelne Schauer (undocumented)
    -12: "lightning",  # einzelne Gewitter (undocumented)
    -13: "snowy",  # einzelne Schneeschauer (undocumented)
    -14: "snowy",  # einzelne Schneeschauer, dazwischen sogar Blitz und Donner (undocumented)
    -15: "snowy-rainy",  # einzelne Schauer, vereinzelt auch Flocken (undocumented)
    -16: "partlycloudy",  # oft sonnig, nur einzelne gewittrige Schauer, teils auch Flocken (undocumented)
    -17: "fog",  # Nebel
    -18: "cloudy",  # stark bewölkt (undocumented)
    -19: "cloudy",  # bedeckt
    -20: "rainy",  # regnerisch
    -21: "snowy",  # Schneefall
    -22: "snowy-rainy",  # Schneeregen
    -23: "pouring",  # Dauerregen (undocumented)
    -24: "snowy",  # starker Schneefall (undocumented)
    -25: "rainy",  # Regenschauer
    -26: "lightning",  # stark bewölkt und einige Gewitter (undocumented)
    -27: "rainy",  # trüb mit einigen Schneeschauern (undocumented)
    -28: "lightning-rainy",  # stark bewölkt, Schneeschauer, dazwischen Blitz und Donner (undocumented)
    -29: "snowy-rainy",  # ab und zu Schneeregen (undocumented)
    -30: "snowy-rainy",  # Schneeregen, einzelne Gewitter (undocumented)
}


def get_condition_from_symbol(symbol_id: int):
    condition = SYMBOL_STATE_MAP.get(symbol_id)
    if condition is None:
        logger.warning("No condition entry for symbol id {}".format(symbol_id))
        condition = STATE_UNAVAILABLE
    return condition


def weak_update(d: MutableMapping, key: str, value: MutableMapping) -> None:
    try:
        existing = d[key]
    except KeyError:
        pass
    else:
        value.update(existing)

    d[key] = value
