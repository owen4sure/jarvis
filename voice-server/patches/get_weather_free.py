import urllib.parse
import urllib.request

from plugins_func.register import register_function, ToolType, ActionResponse, Action

weather_free_desc = {
    "type": "function",
    "function": {
        "name": "get_weather_free",
        "description": "查詢天氣。當使用者問天氣、氣溫、會不會下雨、要不要帶傘時呼叫。使用免費即時天氣資料。",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "地點，例如「台北」「Taipei」「高雄」。沒指定就用「Taipei」。",
                }
            },
            "required": [],
        },
    },
}


import json as _json

# WMO 天氣代碼 → 中文
_WMO = {0: "晴", 1: "大致晴朗", 2: "局部多雲", 3: "陰", 45: "起霧", 48: "霧凇",
        51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "凍雨", 57: "凍雨",
        61: "小雨", 63: "中雨", 65: "大雨", 66: "凍雨", 67: "凍雨",
        71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
        80: "陣雨", 81: "陣雨", 82: "強陣雨", 85: "陣雪", 86: "陣雪",
        95: "雷雨", 96: "雷雨夾冰雹", 99: "強雷雨夾冰雹"}
_RAIN_CODES = {51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99}


# 台灣主要城市座標(open-meteo 地理編碼對繁中地名不友善 → 內建直接查,最穩)
_TW = {
    "台北": (25.05, 121.53), "臺北": (25.05, 121.53), "台北市": (25.05, 121.53),
    "新北": (25.01, 121.46), "板橋": (25.01, 121.46), "新埔": (25.02, 121.47),
    "桃園": (24.99, 121.31), "台中": (24.15, 120.67), "臺中": (24.15, 120.67),
    "台南": (22.99, 120.21), "臺南": (22.99, 120.21), "高雄": (22.62, 120.31),
    "基隆": (25.13, 121.74), "新竹": (24.80, 120.97), "嘉義": (23.48, 120.45),
    "宜蘭": (24.76, 121.75), "花蓮": (23.98, 121.60), "台東": (22.76, 121.14),
    "屏東": (22.67, 120.49), "南投": (23.91, 120.69), "彰化": (24.08, 120.54),
    "雲林": (23.71, 120.43), "苗栗": (24.56, 120.82), "市府": (25.04, 121.57),
    "Taipei": (25.05, 121.53),
}


def _open_meteo(loc):
    """open-meteo：免費、不限流、穩定。台灣城市用內建座標,其他才地理編碼。"""
    lat = lon = None
    name = loc
    for k, v in _TW.items():
        if k in loc:
            lat, lon, name = v[0], v[1], k
            break
    if lat is None:
        gu = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": loc, "count": 1, "language": "en", "format": "json"})
        g = _json.load(urllib.request.urlopen(gu, timeout=10))
        res = (g.get("results") or [None])[0]
        if not res:
            return None
        lat, lon, name = res["latitude"], res["longitude"], res.get("name", loc)
    wu = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,cloud_cover",
        "timezone": "auto"})
    d = _json.load(urllib.request.urlopen(wu, timeout=10))
    c = d.get("current") or {}
    t = c.get("temperature_2m")
    f = c.get("apparent_temperature")
    h = c.get("relative_humidity_2m")
    prec = c.get("precipitation") or 0
    cloud = c.get("cloud_cover")
    # 【關鍵】open-meteo 的 weather_code 對台灣天況常誤判(動不動就冰雹) → 不信它,
    # 改用「降雨量 + 雲量」自己推天況(這兩個值準),才不會冒出台北冰雹這種鬼。
    if prec >= 3:
        cond = "下雨"
    elif prec >= 0.2:
        cond = "局部短暫雨"
    elif cloud is None:
        cond = ""
    elif cloud < 25:
        cond = "晴"
    elif cloud < 55:
        cond = "多雲時晴"
    elif cloud < 85:
        cond = "多雲"
    else:
        cond = "陰"
    umb = "，會下雨記得帶傘" if prec >= 0.2 else ""
    ts = f"{round(t)}度" if t is not None else "未知"
    fs = f"、體感{round(f)}度" if f is not None else ""
    hs = f"，濕度{h}%" if h is not None else ""
    return f"{name}現在{cond}，氣溫{ts}{fs}{hs}{umb}"


@register_function("get_weather_free", weather_free_desc, ToolType.SYSTEM_CTL)
def get_weather_free(conn, location: str = "Taipei"):
    loc = location or "Taipei"
    # 主：open-meteo(穩)
    try:
        say = _open_meteo(loc)
        if say:
            return ActionResponse(action=Action.RESPONSE, result=say, response=say)
    except Exception:
        pass
    # 備援：wttr.in(偶爾限流/超時,所以當備援)
    try:
        fmt = "%C|%t|%f|%h|%p"  # 天況|溫度|體感|濕度|降雨
        url = ("https://wttr.in/" + urllib.parse.quote(loc)
               + "?format=" + urllib.parse.quote(fmt) + "&lang=zh-tw")
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        data = urllib.request.urlopen(req, timeout=12).read().decode("utf-8").strip()
        parts = data.split("|")
        cond = parts[0] if len(parts) > 0 else ""
        # wttr.in 偶爾回英文天況 → 補中文對照
        _cond_map = {
            "Sunny": "晴", "Clear": "晴朗", "Partly cloudy": "局部多雲",
            "Cloudy": "多雲", "Overcast": "陰天", "Mist": "薄霧",
            "Fog": "起霧", "Smoky haze": "煙霾", "Haze": "霾",
            "Patchy rain possible": "局部有雨", "Light rain": "小雨",
            "Moderate rain": "中雨", "Heavy rain": "大雨", "Light drizzle": "毛毛雨",
            "Thundery outbreaks possible": "可能有雷雨", "Rain": "下雨",
        }
        cond = _cond_map.get(cond, cond)
        temp = parts[1].replace("+", "").strip() if len(parts) > 1 else ""
        feel = parts[2].replace("+", "").strip() if len(parts) > 2 else ""
        hum = parts[3] if len(parts) > 3 else ""
        rain = parts[4] if len(parts) > 4 else ""
        # 正確解析降雨量數字（原本只看第一個字，"0.3 mm" 會誤判成不會下雨）
        try:
            _rain_mm = float(str(rain).split()[0])
        except Exception:
            _rain_mm = 0.0
        umbrella = ("，可能會下雨，記得帶傘" if _rain_mm > 0 else "")
        say = f"{loc}現在{cond}，氣溫{temp}、體感{feel}，濕度{hum}{umbrella}"
        return ActionResponse(action=Action.RESPONSE, result=data, response=say)
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result=str(e),
                              response="天氣服務剛剛連不上，等等再問我")
