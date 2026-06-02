import json
import httpx
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRETS_DIR = BASE_DIR / "secrets"


def get_location():
    response = httpx.get("https://ipinfo.io/json")
    data = response.json()
    lat, lon = data["loc"].split(",")
    return float(lat), float(lon)


def get_weather(lat: float, lon: float, api_key: str):
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": "metric",
        "lang": "ja",
    }
    response = httpx.get(url, params=params)
    return response.json()


def main():
    with open(SECRETS_DIR / "weather.json") as f:
        api_keys = json.load(f)

    lat, lon = get_location()
    print(f"現在地: 緯度{lat}, 経度{lon}")

    weather = get_weather(lat, lon, api_keys["openweathermap"])
    print(f"天気: {weather['weather'][0]['description']}")
    print(f"気温: {weather['main']['temp']}℃")


if __name__ == "__main__":
    main()
