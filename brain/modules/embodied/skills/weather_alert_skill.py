"""Shake-to-check-weather, from the Stack-chan "感知世界" wish list.

Shake the StackChan (IMU event) -> Hermes fetches current weather +
UV index + air quality from Open-Meteo (free, no API key) -> speaks a
short summary and flashes a red LED if rain is likely.

This is a template for "感知世界" style skills: fetch some external
signal and turn it into speech + LED feedback.
"""

import json
import urllib.request

from .. import config

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,precipitation,weather_code,uv_index"
    "&timezone={tz}"
)

AIR_QUALITY_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    "?latitude={lat}&longitude={lon}&current=us_aqi"
)

# WMO weather codes: 51-67/80-99 roughly mean rain/showers
RAIN_CODES = set(range(51, 68)) | set(range(80, 100))


def register(ctx):
    ctx.sensory.on_event("sensor/imu", _on_shake(ctx))


def _on_shake(ctx):
    def handler(payload):
        if payload.get("event") != "shake":
            return
        try:
            summary, is_raining = _fetch_weather()
        except Exception as e:
            ctx.speak("我看不清楚外面的天氣，網路好像有問題。")
            print(f"⚠️ [WeatherSkill] 取得天氣失敗: {e}")
            return

        ctx.speak(summary)
        if is_raining:
            ctx.send_command("LED_RED_BLINK")
    return handler


def _uv_level(uv_index):
    if uv_index >= 11:
        return "極危險"
    if uv_index >= 8:
        return "危險"
    if uv_index >= 6:
        return "高"
    if uv_index >= 3:
        return "中等"
    return "低"


def _aqi_level(aqi):
    if aqi > 150:
        return "對所有人不健康"
    if aqi > 100:
        return "對敏感族群不健康"
    if aqi > 50:
        return "中等"
    return "良好"


def _fetch_weather():
    loc = config.LOCATION
    url = WEATHER_URL.format(lat=loc["latitude"], lon=loc["longitude"], tz=loc["timezone"])
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.load(resp)

    current = data["current"]
    temp = current["temperature_2m"]
    code = current["weather_code"]
    uv_index = current.get("uv_index")
    is_raining = code in RAIN_CODES

    summary = f"{loc['name']} 現在氣溫 {temp} 度"
    summary += "，可能會下雨，記得帶傘喔。" if is_raining else "，天氣還不錯。"

    if uv_index is not None:
        summary += f"\n紫外線指數 {uv_index}（{_uv_level(uv_index)}）"

    try:
        aqi_url = AIR_QUALITY_URL.format(lat=loc["latitude"], lon=loc["longitude"])
        with urllib.request.urlopen(aqi_url, timeout=5) as resp:
            aqi_data = json.load(resp)
        aqi = aqi_data["current"]["us_aqi"]
        summary += f"\n空氣品質指數 (US AQI) {aqi}（{_aqi_level(aqi)}）"
    except Exception as e:
        print(f"⚠️ [WeatherSkill] 取得空氣品質失敗: {e}")

    return summary, is_raining
