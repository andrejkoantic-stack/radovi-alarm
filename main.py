
import json
import os
import re
import unicodedata
from datetime import datetime
import requests
import feedparser
from bs4 import BeautifulSoup

CONFIG_FILE = "config.json"
SEEN_FILE = "seen.json"
DATA_FILE = "data.json"
HEADERS = {"User-Agent": "RadoviAlertApp/1.0"}

KLJUCNE_RIJECI = [
    "radovi", "radova", "bez struje", "bez vode", "iskljucenj",
    "zatvara se", "zatvoren", "promet", "kvar", "vodoopskrb",
    "plin", "elektroprimorje", "hep", "ponikve", "vik"
]

def normaliziraj(tekst):
    tekst = tekst.lower()
    return unicodedata.normalize("NFKD", tekst).encode("ascii", "ignore").decode("utf-8")

def dohvati_koordinate(adresa):
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": adresa, "format": "json", "limit": 1},
        headers=HEADERS,
        timeout=15
    )
    data = r.json()
    if not data:
        raise ValueError(f"Adresa nije pronađena: {adresa}")
    return float(data[0]["lat"]), float(data[0]["lon"])

def dohvati_ulice_u_radijusu(lat, lon, radijus):
    query = f"""
    [out:json][timeout:25];
    way(around:{radijus},{lat},{lon})["highway"]["name"];
    out tags;
    """
    r = requests.post("https://overpass-api.de/api/interpreter", data={"data": query}, headers=HEADERS, timeout=30)
    data = r.json()
    return sorted({el["tags"]["name"] for el in data.get("elements", []) if "name" in el.get("tags", {})})

def pripremi_korijene_ulica(ulice):
    stop = {"ulica", "trg", "put", "cesta", "prilaz", "stube", "setaliste", "prolaz", "avenija", "obala", "dr", "sv", "svetog"}
    korijeni = {}
    for u in ulice:
        rijeci = [w for w in re.findall(r"[a-z]+", normaliziraj(u)) if w not in stop and len(w) > 3]
        if rijeci:
            korijeni[u] = rijeci[-1][:6]
    return korijeni

def ucitaj_json(putanja, default):
    if os.path.exists(putanja):
        with open(putanja, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def spremi_json(putanja, podaci):
    with open(putanja, "w", encoding="utf-8") as f:
        json.dump(podaci, f, ensure_ascii=False, indent=2)

def posalji_onesignal(naslov, poruka, link=""):
    app_id = os.environ.get("ONESIGNAL_APP_ID")
    api_key = os.environ.get("ONESIGNAL_REST_API_KEY")
    if not app_id or not api_key:
        print("Nedostaju OneSignal ključevi u GitHub Secrets.")
        return

    payload = {
        "app_id": app_id,
        "included_segments": ["Subscribed Users"],
        "headings": {"en": naslov, "hr": naslov},
        "contents": {"en": poruka, "hr": poruka}
    }
    if link:
        payload["url"] = link

    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json; charset=utf-8"
    }
    r = requests.post("https://api.onesignal.com/notifications", json=payload, headers=headers, timeout=15)
    print(f"OneSignal odgovor: {r.status_code}")

def provjeri_tekst(tekst, korijeni_ulica, treba_kljucnu_rijec=True):
    norm = normaliziraj(tekst)
    if treba_kljucnu_rijec and not any(k in norm for k in KLJUCNE_RIJECI):
        return []
    return [u for u, korijen in korijeni_ulica.items() if re.search(rf"\b{re.escape(korijen)}", norm)]

def main():
    cfg = ucitaj_json(CONFIG_FILE, {})
    lat, lon = dohvati_koordinate(cfg["adresa"])
    ulice = dohvati_ulice_u_radijusu(lat, lon, cfg["radijus_m"])
    korijeni = pripremi_korijene_ulica(ulice)

    vidjeno = set(ucitaj_json(SEEN_FILE, []))
    stari_podaci = ucitaj_json(DATA_FILE, {"alarmi": []})
    alarmi = stari_podaci.get("alarmi", [])
    sada = datetime.now().strftime("%d.%m.%Y. %H:%M")

    # 1. RSS izvori
    for rss_url in cfg["rss_izvori"]:
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:15]:
                link = entry.get("link", "")
                if link in vidjeno:
                    continue
                tekst = f"{entry.get('title', '')} {entry.get('summary', '')}"
                pogoci = provjeri_tekst(tekst, korijeni, treba_kljucnu_rijec=True)
                if pogoci:
                    naslov = f"Radovi: {', '.join(pogoci[:2])}"
                    poruka = entry.get("title", "Novi radovi u blizini")
                    posalji_onesignal(naslov, poruka, link)
                    vidjeno.add(link)
                    alarmi.insert(0, {"vrijeme": sada, "naslov": naslov, "opis": poruka, "ulice": pogoci, "link": link})
        except Exception as e:
            print(f"RSS greška ({rss_url}): {e}")

    # 2. HEP ODS
    try:
        r = requests.get(cfg["hep_url"], headers=HEADERS, timeout=15)
        tekst_stranice = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        pogoci = provjeri_tekst(tekst_stranice, korijeni, treba_kljucnu_rijec=False)
        if pogoci:
            hep_id = f"HEP-{datetime.now().strftime('%Y-%m-%d')}-{''.join(sorted(pogoci))}"
            if hep_id not in vidjeno:
                naslov = "HEP: Bez struje u blizini"
                poruka = f"Ulice na HEP listi: {', '.join(pogoci)}"
                posalji_onesignal(naslov, poruka, cfg["hep_url"])
                vidjeno.add(hep_id)
                alarmi.insert(0, {"vrijeme": sada, "naslov": naslov, "opis": poruka, "ulice": pogoci, "link": cfg["hep_url"]})
    except Exception as e:
        print(f"HEP greška: {e}")

    spremi_json(SEEN_FILE, list(vidjeno))
    spremi_json(DATA_FILE, {
        "zadnja_provjera": sada,
        "adresa": cfg["adresa"],
        "radijus_m": cfg["radijus_m"],
        "broj_ulica": len(ulice),
        "ulice": ulice,
        "alarmi": alarmi[:20]
    })

if __name__ == "__main__":
    main()
