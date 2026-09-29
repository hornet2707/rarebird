#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
レアバード・スコープ — 撮影計画エンジン
=====================================

GitHub Actions から5分おきに実行され、撮影拠点空港（既定: 羽田・成田）に
「今日・明日、何時に、どの滑走路で、撮る価値のある機体が発着するか」を計算し、
静的ページ（GitHub Pages）用のJSONと通知を出す。

外部ライブラリ不要（Python 3.10+ 標準ライブラリのみ）。

データ源（すべて無料・APIキー不要）
- ADS-B          : adsb.lol v2 API（予備: airplanes.live）… 位置・機体・便名
- 経路           : adsb.lol routeset … 便名 → 出発地/到着地
- 羽田の時刻表   : 羽田空港旅客ターミナル公式サイトの内部API（今日・明日の全便）
- 気象           : aviationweather.gov（METAR/TAF）、Open-Meteo（時間別予報）
- 特別塗装機台帳 : 公開の特別塗装機一覧ページ（Flightradar24機体ページへのリンク一覧）
- 通知           : ntfy.sh（無料のプッシュ通知。Android/iPhoneアプリあり）

使い方
    python3 rarebird.py --site site            # 本番
    python3 rarebird.py --site out --fixtures fixtures --now 2026-09-28T16:45:00+09:00  # オフライン試験
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import time
import traceback
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

VERSION = "2.5.0"

# --------------------------------------------------------------------------
# 既定設定（config.json で上書きできる）
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "airports": ["RJTT", "RJAA"],
    "timezone": "Asia/Tokyo",
    "scan_radius_nm": 45,       # 発着の記録・滑走路の判定に使う範囲
    "wide_radius_nm": 150,      # 希少機種・軍用機の接近に気づく範囲
    "global_type_minutes": 30,  # 希少機種を世界中から型式で探す間隔
    # 国内線にはまず入らない機材（国内線を飛んでいたら注目機にする）。国際線ではふつうの機材なので国内線に限る
    "domestic_unusual_types": ["B77W", "A35K", "A388", "B744", "B748", "B77L", "B77F", "A333", "A332",
                               "A339", "B764", "B753", "B752"],
    # その空港にはふつう来ない機材（その空港に発着する便なら注目機にする）
    "airport_unusual_types": {
        "RJTT": ["DH8D", "DH8A", "DH8B", "DH8C", "AT43", "AT45", "AT46", "AT72", "AT75", "AT76", "SF34", "D228",
                 "E170", "E75S", "E75L", "E190", "CRJ2", "CRJ7", "CRJ9"],
        "RJAA": ["DH8A", "DH8B", "DH8C", "AT43", "AT45", "AT46", "AT72", "AT75", "AT76", "SF34", "D228"],
    },
    "domestic_unusual_minutes": 10,  # 上の機材を国内で探す間隔（分）
    "history_days": 14,
    "rare_types": [
        "A388", "B741", "B742", "B743", "B744", "B748", "B74F", "B74R", "B74S", "BLCF",
        "A124", "A225", "IL76", "IL96", "A342", "A343", "A345", "A346", "MD11", "B77L",
        "A3ST", "A337", "C17", "C5M", "C30J", "C130", "KC46", "K35R", "A400", "P8", "E3TF",
        "E767", "B703", "DC10", "A310", "AN12", "AN22", "B752", "B753", "CONC",
    ],
    "heavy_types": [
        "A388", "B744", "B748", "B74F", "B74S", "B77W", "B77L", "B77F", "A124", "MD11",
        "A346", "A345", "C5M", "C17", "IL76", "B763F", "A35K",
    ],
    "common_operators": [
        "ANA", "JAL", "ADO", "SKY", "SNJ", "JJP", "APJ", "SFJ", "JTA", "RAC", "NCA", "AKX",
        "WAJ", "IBX", "FDA", "ORC", "SJO", "JAC", "HAC", "AMX", "KZA", "TZP", "NTH", "SRA",
        "UAL", "DAL", "AAL", "HAL", "ACA", "KAL", "AAR", "CCA", "CES", "CSN", "CAL", "EVA",
        "SIA", "THA", "CPA", "HVN", "PAL", "CEB", "GIA", "MAS", "QFA", "ANZ", "AFR", "BAW",
        "DLH", "KLM", "SWR", "FIN", "SAS", "UAE", "QTR", "ETD", "THY", "AIC", "VJC", "HDA",
        "TGW", "SJX", "FDX", "UPS", "GTI", "CKS", "ABW", "HKE", "CSH", "ASA", "TTW", "ITY",
        "XAX", "CQH", "GCR", "DKH", "JJA", "TWB", "JNA", "ESR", "ABL", "CRK", "HKC", "TAX",
        "APG", "AXM", "ETH", "LOT", "AUA", "SVA", "MDA", "KZR", "MGL", "OMA", "UZB", "SCO",
    ],
    # 常に監視する登録記号（台帳の自動更新とは無関係に残る）
    "pinned_regs": {
        "80-1111": "政府専用機（B777-300ER）",
        "80-1112": "政府専用機（B777-300ER）",
        "JA381A": "ANA FLYING HONU 1号機（A380）",
        "JA382A": "ANA FLYING HONU 2号機（A380）",
        "JA383A": "ANA FLYING HONU 3号機（A380）",
    },
    "ignored_regs": [],
    "livery_sources": ["https://viaflight-jp.com/special-livery-japan/",
                       "https://viaflight-jp.com/special-livery-world/"],
    "notify": {
        "server": "https://ntfy.sh",
        "morning_hour": 6,
        "evening_hour": 21,
        "live_minutes": 90,
        "quiet_start": 23,
        "quiet_end": 5,
    },
    "adsb_providers": ["https://api.adsb.lol/v2", "https://api.airplanes.live/v2"],
    # 政府・要人機・軍用機・珍しい機体のデータベース（Plane-Alert-DB, ODbL）
    "plane_alert": {
        "enabled": True,
        "groups": ["vip", "mil", "rare"],       # 使う分類のまとまり（"public" で海保・警察・消防なども）
        "extra_categories": [],                  # 個別に足す分類（Plane-Alert-DB の Category 名）
        "exclude_categories": [],                # 個別に外す分類
        "global_categories": ["Head of State", "Governments", "Royal Aircraft", "Radiohead"],
        "regular_days": 4,                       # 直近14日でこの日数以上来た機体は「常連」として外す
        "url": "https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/main/plane-alert-db.csv",
    },
}

LIGHT_TYPES = {
    "DA40", "DA42", "R22", "R44", "R66", "C150", "C152", "C172", "C182", "C206", "C208",
    "SR20", "SR22", "P28A", "PA46", "EC25", "EC35", "EC45", "EC30", "A109", "A119",
    "H500", "AS50", "AS55", "AS65", "B06", "B407", "B429", "S76", "D228", "A7", "BE20",
    "BE35", "BE36", "BE9L", "B350", "PC12", "TBM7", "TBM8", "TBM9", "M20P", "GLID",
}
MIL_TYPES = {
    "C17", "C5M", "C30J", "C130", "KC46", "K35R", "A400", "P8", "E3TF", "R135", "H60",
    "E6", "B52", "C40", "C32", "VC25", "E4", "KC30", "C2", "P1", "KC10", "E2", "V22",
    "CH47", "UH1", "U125", "U4",
}
DOMESTIC_JP_PREFIX = ("RJ", "RO")

# 空港の座標・呼称（羽田APIの都市名 → IATA）。OurAirports (Public Domain) 由来
GEO = {"ja2iata":{"三沢":"MSJ","中標津":"SHB","佐賀":"HSG","八丈島":"HAC","出雲":"IZO","函館":"HKD","北九州":"KKJ","南紀白浜":"SHM","名古屋（中部）":"NGO","大分":"OIT","大阪（伊丹）":"ITM","大阪（関空）":"KIX","大館能代":"ONJ","奄美":"ASJ","女満別":"MMB","宮古":"MMY","宮古（下地島）":"SHI","宮崎":"KMI","富山":"TOY","小松":"KMQ","山口宇部":"UBJ","山形":"GAJ","岡山":"OKJ","岩国":"IWK","帯広":"OBO","広島":"HIJ","庄内":"SYO","徳島":"TKS","旭川":"AKJ","札幌（新千歳）":"CTS","松山":"MYJ","沖縄（那覇）":"OKA","熊本":"KMJ","石垣":"ISG","神戸":"UKB","福岡":"FUK","秋田":"AXT","稚内":"WKJ","米子":"YGJ","紋別":"MBE","能登":"NTQ","萩・石見":"IWJ","釧路":"KUH","長崎":"NGS","青森":"AOJ","高松":"TAK","高知":"KCZ","鳥取":"TTJ","鹿児島":"KOJ","アトランタ":"ATL","イスタンブール":"IST","ウィーン":"VIE","クアラルンプール":"KUL","グアム":"GUM","コペンハーゲン":"CPH","サンフランシスコ":"SFO","シアトル":"SEA","シカゴ（ORD）":"ORD","シドニー":"SYD","シンガポール":"SIN","ジャカルタ":"CGK","ストックホルム（ARN）":"ARN","ソウル（仁川）":"ICN","ソウル（金浦）":"GMP","ダラス（DFW）":"DFW","デトロイト":"DTW","デリー":"DEL","トロント":"YYZ","ドバイ":"DXB","ドーハ":"DOH","ニューアーク":"EWR","ニューヨーク（JFK)":"JFK","ハノイ":"HAN","バンクーバー":"YVR","バンコク（BKK）":"BKK","パリ（CDG）":"CDG","ヒューストン":"IAH","フランクフルト":"FRA","ヘルシンキ":"HEL","ホノルル":"HNL","ホーチミン":"SGN","マニラ":"MNL","ミネアポリス":"MSP","ミュンヘン":"MUC","ミラノ(MXP)":"MXP","ムンバイ":"BOM","ロサンゼルス":"LAX","ロンドン（LHR）":"LHR","ローマ（FCO）":"FCO","ワシントンDC":"IAD","上海（浦東）":"PVG","上海（虹橋）":"SHA","北京（大興）":"PKX","北京（首都）":"PEK","台北（松山）":"TSA","台北（桃園）":"TPE","大連":"DLC","天津":"TSN","広州":"CAN","深圳":"SZX","青島":"TAO","香港":"HKG"},"airports":{"YVR":["CYVR",49.1939,-123.184],"YYZ":["CYYZ",43.6759,-79.6294],"FRA":["EDDF",50.0267,8.5584],"CGN":["EDDK",50.8659,7.1427],"MUC":["EDDM",48.3538,11.7861],"LEJ":["EDDP",51.4207,12.2327],"HEL":["EFHK",60.3184,24.9633],"LHR":["EGLL",51.4707,-0.4599],"AMS":["EHAM",52.3086,4.7639],"CPH":["EKCH",55.6179,12.656],"LUX":["ELLX",49.6268,6.2121],"WAW":["EPWA",52.1657,20.9671],"ARN":["ESSA",59.6485,17.9288],"ATL":["KATL",33.6367,-84.4281],"BOS":["KBOS",42.362,-71.0079],"CVG":["KCVG",39.0488,-84.6678],"DFW":["KDFW",32.8968,-97.038],"DTW":["KDTW",42.2138,-83.3538],"EWR":["KEWR",40.6894,-74.1705],"IAD":["KIAD",38.9445,-77.4558],"IAH":["KIAH",29.9844,-95.3414],"JFK":["KJFK",40.6394,-73.7793],"LAX":["KLAX",33.9425,-118.408],"MEM":["KMEM",35.0438,-89.9763],"MSP":["KMSP",44.8801,-93.2217],"ORD":["KORD",41.9786,-87.9048],"SDF":["KSDF",38.1706,-85.7351],"SEA":["KSEA",47.4479,-122.3103],"SFO":["KSFO",37.6198,-122.3748],"CDG":["LFPG",49.009,2.5541],"MXP":["LIMC",45.6306,8.7281],"FCO":["LIRF",41.8045,12.252],"TLV":["LLBG",32.0114,34.8867],"VIE":["LOWW",48.1103,16.5697],"ZRH":["LSZH",47.4581,8.5481],"IST":["LTFM",41.2749,28.7321],"AKL":["NZAA",-37.012,174.7863],"AUH":["OMAA",24.441,54.6492],"DXB":["OMDB",25.2498,55.371],"DOH":["OTHH",25.2731,51.6081],"ANC":["PANC",61.179,-149.9926],"SPN":["PGSN",15.1194,145.7288],"GUM":["PGUM",13.485,144.7973],"KOA":["PHKO",19.7388,-156.0456],"HNL":["PHNL",21.3184,-157.9257],"KHH":["RCKH",22.5771,120.35],"TSA":["RCSS",25.0672,121.5528],"TPE":["RCTP",25.0777,121.233],"NRT":["RJAA",35.7686,140.3887],"KIX":["RJBB",34.4273,135.244],"SHM":["RJBD",33.6622,135.364],"UKB":["RJBE",34.6328,135.224],"OBO":["RJCB",42.7333,143.217],"CTS":["RJCC",42.7748,141.6904],"HKD":["RJCH",41.77,140.822],"KUH":["RJCK",43.041,144.193],"MMB":["RJCM",43.8806,144.164],"SHB":["RJCN",43.5775,144.96],"WKJ":["RJCW",45.4042,141.801],"UBJ":["RJDC",33.93,131.279],"MBE":["RJEB",44.3039,143.404],"AKJ":["RJEC",43.6708,142.447],"FUK":["RJFF",33.5859,130.451],"KOJ":["RJFK",31.8034,130.719],"KMI":["RJFM",31.8772,131.449],"OIT":["RJFO",33.4794,131.737],"KKJ":["RJFR",33.8459,131.035],"HSG":["RJFS",33.1497,130.302],"KMJ":["RJFT",32.8373,130.855],"NGS":["RJFU",32.9169,129.914],"NGO":["RJGG",34.8584,136.805],"ASJ":["RJKA",28.4306,129.713],"KMQ":["RJNK",36.3934,136.4069],"TOY":["RJNT",36.6484,137.1874],"NTQ":["RJNW",37.2931,136.962],"HIJ":["RJOA",34.4361,132.919],"OKJ":["RJOB",34.7569,133.855],"IZO":["RJOC",35.4136,132.89],"YGJ":["RJOH",35.4922,133.236],"IWK":["RJOI",34.1463,132.2472],"KCZ":["RJOK",33.5452,133.6702],"MYJ":["RJOM",33.8269,132.7001],"ITM":["RJOO",34.7809,135.4408],"TTJ":["RJOR",35.5301,134.165],"TKS":["RJOS",34.1326,134.6078],"TAK":["RJOT",34.215,134.0155],"IWJ":["RJOW",34.6764,131.79],"AOJ":["RJSA",40.7338,140.6895],"GAJ":["RJSC",38.4119,140.371],"AXT":["RJSK",39.6156,140.219],"MSJ":["RJSM",40.7032,141.368],"ONJ":["RJSR",40.1919,140.371],"SYO":["RJSY",38.8122,139.787],"HAC":["RJTH",33.1148,139.7856],"HND":["RJTT",35.5497,139.787],"PUS":["RKPK",35.1795,128.938],"ICN":["RKSI",37.4691,126.451],"GMP":["RKSS",37.5583,126.791],"OKA":["ROAH",26.1924,127.6398],"ISG":["ROIG",24.3964,124.245],"MMY":["ROMY",24.7828,125.295],"SHI":["RORS",24.8267,125.145],"MNL":["RPLL",14.5086,121.02],"CEB":["RPVM",10.3093,123.9797],"KHV":["UHHH",48.5283,135.1886],"UUS":["UHSS",46.8855,142.7175],"VVO":["UHWW",43.3963,132.1482],"IKT":["UIII",52.2667,104.3956],"SVO":["UUEE",55.9769,37.4112],"BOM":["VABB",19.0887,72.8679],"HKG":["VHHH",22.3118,113.9149],"DEL":["VIDP",28.5556,77.0952],"MFM":["VMMC",22.1496,113.592],"DMK":["VTBD",13.9126,100.607],"BKK":["VTBS",13.6811,100.747],"DAD":["VVDN",16.0439,108.199],"HAN":["VVNB",21.2212,105.807],"SGN":["VVTS",10.8188,106.652],"DPS":["WADD",-8.7484,115.1671],"CGK":["WIII",-6.1256,106.656],"KUL":["WMKK",2.7456,101.71],"PEN":["WMKP",5.2963,100.2762],"SIN":["WSSS",1.3502,103.994],"BNE":["YBBN",-27.3842,153.117],"OOL":["YBCG",-28.166,153.5066],"CNS":["YBCS",-16.8789,145.7495],"MEL":["YMML",-37.6707,144.8379],"PER":["YPPH",-31.9403,115.967],"SYD":["YSSY",-33.9461,151.177],"PEK":["ZBAA",40.0773,116.5967],"PKX":["ZBAD",39.5013,116.414],"TSN":["ZBTJ",39.1244,117.346],"CAN":["ZGGG",23.3924,113.299],"SZX":["ZGSZ",22.6395,113.8033],"CGO":["ZHCC",34.5265,113.8492],"WUH":["ZHHH",30.7748,114.2137],"HAK":["ZJHK",19.9349,110.459],"XIY":["ZLXY",34.4422,108.7624],"ULN":["ZMUB",47.8431,106.767],"KMG":["ZPPP",25.1103,102.9367],"XMN":["ZSAM",24.5439,118.1275],"FOC":["ZSFZ",25.9293,119.6725],"HGH":["ZSHC",30.2361,120.4289],"NKG":["ZSNJ",31.735,118.8659],"PVG":["ZSPD",31.1434,121.805],"TAO":["ZSQD",36.362,120.0882],"SHA":["ZSSS",31.1981,121.3343],"CKG":["ZUCK",29.7123,106.6519],"CTU":["ZUUU",30.5583,103.946],"HRB":["ZYHB",45.6234,126.25],"DLC":["ZYTL",38.9657,121.5385],"SHE":["ZYTX",41.6398,123.4837],"YNJ":["ZYYJ",42.8828,129.451]}}

# 滑走路の端点（OurAirports）。方位は端点座標から計算する（元データの誤記対策）
RUNWAYS = {
    "RJTT": [["04", 35.549015, 139.761274, "22", 35.567459, 139.777114, 8202],
             ["05", 35.524001, 139.803469, "23", 35.540598, 139.822125, 8202],
             ["16L", 35.565897, 139.78655, "34R", 35.53969, 139.805142, 11024],
             ["16R", 35.560452, 139.768734, "34L", 35.536591, 139.785672, 9843]],
    "RJAA": [["16L", 35.8027, 140.380005, "34R", 35.785801, 140.391998, 8202],
             ["16R", 35.774399, 140.367996, "34L", 35.743301, 140.391006, 13123]],
}
AIRPORT_INFO = {
    "RJTT": {"name": "羽田", "iata": "HND", "lat": 35.5533, "lon": 139.7811, "timetable": "haneda"},
    "RJAA": {"name": "成田", "iata": "NRT", "lat": 35.7647, "lon": 140.3864, "timetable": "narita"},
}
# 気象庁の府県天気予報の区域（羽田=東京都 東京地方、成田=千葉県 北西部）
JMA_AREA = {"RJTT": ("130000", "130010"), "RJAA": ("120000", "120010")}
# 運用を切り替える追い風の目安（kt）。これ以下なら今の運用が続くとみなす
TAILWIND_SWITCH_KT = 5.0
# 軍用機の行き先候補（民間機の判定には使わない）
MIL_FIELDS = {
    "RJTY": ("横田", 35.7485, 139.3486), "RJTA": ("厚木", 35.4546, 139.4500),
    "RJTJ": ("入間", 35.8419, 139.4106), "RJTK": ("木更津", 35.3983, 139.9100),
    "RJAH": ("百里", 36.1814, 140.4147), "RJTL": ("下総", 35.7988, 140.0114),
}

UA = "Mozilla/5.0 (compatible; RareBirdScope/2.0; +https://github.com/)"


# --------------------------------------------------------------------------
# 小道具
# --------------------------------------------------------------------------
def nm_between(lat1, lon1, lat2, lon2):
    """大円距離（海里）"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def ang_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def local_xy(lat0, lon0, lat, lon):
    """(lat0,lon0) 基準の平面座標（海里）。x=東, y=北"""
    return ((lon - lon0) * 60.0 * math.cos(math.radians(lat0)), (lat - lat0) * 60.0)


def num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, pretty=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def hhmm_ok(s):
    """'7:05' / '07:05' → '07:05'。時刻でなければ空文字"""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(s or ""))
    if not m or int(m.group(1)) > 29 or int(m.group(2)) > 59:
        return ""
    return "%02d:%s" % (int(m.group(1)) % 24, m.group(2))


def clean_cs(s):
    return (s or "").strip().upper()


def fl_norm(s):
    """便名の正規化 'NH 060' → 'NH60'"""
    s = (s or "").upper().replace(" ", "")
    m = re.match(r"^([A-Z0-9]{2}?[A-Z]?)0*(\d+)([A-Z]?)$", s)
    if not m:
        return s
    return f"{m.group(1)}{m.group(2)}{m.group(3)}"


class Log:
    def __init__(self):
        self.errors = []
        self.notes = []

    def err(self, where, e):
        msg = f"{where}: {type(e).__name__}: {e}"
        print("ERROR", msg, file=sys.stderr)
        self.errors.append(msg[:300])

    def note(self, msg):
        print(msg)
        self.notes.append(msg[:300])


LOG = Log()


# --------------------------------------------------------------------------
# 通信（テスト時は Fixture に差し替え）
# --------------------------------------------------------------------------
class Net:
    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = 0

    def _req(self, url, data=None, headers=None, timeout=25):
        h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
        h.update(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        last = None
        for attempt in range(3):
            try:
                self.calls += 1
                req = urllib.request.Request(url, data=body, headers=h)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (400, 401, 403, 404):
                    break
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(1.5 * (attempt + 1))
        raise last

    def get_json(self, url, **kw):
        return json.loads(self._req(url, **kw).decode("utf-8"))

    def post_json(self, url, data, **kw):
        return json.loads(self._req(url, data=data, **kw).decode("utf-8"))

    def get_text(self, url, **kw):
        return self._req(url, **kw).decode("utf-8", "replace")

    # --- ADS-B ---
    def adsb(self, path):
        last = None
        for base in self.cfg["adsb_providers"]:
            try:
                d = self.get_json(base.rstrip("/") + path)
                return d.get("ac") or d.get("aircraft") or []
            except Exception as e:  # noqa: BLE001
                last = e
        raise last or RuntimeError("no provider")

    def area(self, lat, lon, r):
        return self.adsb(f"/point/{lat:.4f}/{lon:.4f}/{int(r)}")

    def regs(self, regs):
        """監視機の全世界照会。まず一括（カンマ区切り）、だめなら重要な機体から1機ずつ"""
        out = []
        try:
            for i in range(0, len(regs), 35):
                out.extend(self.adsb("/reg/" + ",".join(regs[i:i + 35])))
            return out
        except Exception as e:  # noqa: BLE001
            LOG.note(f"監視機の一括照会に失敗（{type(e).__name__}）。1機ずつに切り替え")
        for r in regs[:15]:
            try:
                out.extend(self.adsb("/reg/" + r))
            except Exception:  # noqa: BLE001
                pass
        return out

    def types(self, types):
        """希少機種を型式で世界中から探す（カンマ区切りで複数指定）"""
        out = []
        for i in range(0, len(types), 12):
            out.extend(self.adsb("/type/" + ",".join(types[i:i + 12])))
        return out

    def squawk(self, code):
        """そのスコークを出している機体を世界中から"""
        return self.adsb(f"/sqk/{code}")

    def reports(self, server, topic, since):
        """画面・通知から送られた誤り報告（ntfy に12時間保管される）を受け取る"""
        return self.get_text(f"{server.rstrip('/')}/{topic}/json?poll=1&since={since}", timeout=20)

    def hexes(self, hexes):
        """機体番号（24bit）で世界中から。カンマ区切りで30機ずつ（長すぎると 403 になる）"""
        out = []
        for i in range(0, len(hexes), 30):
            out.extend(self.adsb("/hex/" + ",".join(hexes[i:i + 30])))
        return out

    def routeset(self, planes):
        out = []
        for i in range(0, len(planes), 100):
            raw = self._req("https://api.adsb.lol/api/0/routeset", data={"planes": planes[i:i + 100]})
            try:
                out.extend(json.loads(raw))
            except ValueError:
                snippet = raw[:120].decode("utf-8", "replace").replace("\n", " ")
                raise RuntimeError(f"JSONでない応答（{len(raw)}バイト）: {snippet!r}")
        return out

    def vrs_routes(self, airline):
        """VRS standing-data（adsb.lol の経路データの元）から航空会社ごとの便名→経路表"""
        url = f"https://raw.githubusercontent.com/vradarserver/standing-data/main/routes/schema-01/{airline[0]}/{airline}-all.csv"
        return self.get_text(url, timeout=30)

    # --- 羽田 公式時刻表（内部API。仕様変更で止まる可能性あり） ---
    def haneda(self, flight_type, arrival_type, ymd):
        body = {"flightType": flight_type, "arrivalType": arrival_type, "searchDt": ymd,
                "airportCodes": [], "airlineCodes": [], "flightNumber": "", "status": []}
        hdr = {"X-Requested-With": "XMLHttpRequest", "Origin": "https://tokyo-haneda.com",
               "Referer": "https://tokyo-haneda.com/flight/flightInfo_dms.html",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        d = self.post_json("https://tokyo-haneda.com/app/api/v2/flight/search", body, headers=hdr, timeout=40)
        return d.get("flightlists") or []

    # --- 成田 公式フライト情報（内部API。旅客便のみ・仕様変更で止まる可能性あり） ---
    def narita(self, dom_inter, dep_arr, date_iso):
        hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/128.0 Safari/537.36",
               "Accept": "application/json, text/plain, */*", "Accept-Language": "ja",
               "Referer": f"https://www.narita-airport.jp/ja/flight/{'arr' if dep_arr == 'A' else 'dep'}-search/"}
        out = []
        for page in range(6):
            url = (f"https://www.narita-airport.jp/api/bff/searchFlight/?locale=ja&domInter={dom_inter}"
                   f"&flightDepArr={dep_arr}&date={date_iso}&page={page}&time=00%3A00&size=500")
            d = self.get_json(url, headers=hdr, timeout=40)
            f = d.get("flights") or {}
            out.extend(f.get("data") or [])
            if not f.get("hasNextPage"):
                break
        return out

    # --- 気象 ---
    def metar(self, ids):
        return self.get_json(f"https://aviationweather.gov/api/data/metar?ids={','.join(ids)}&format=json&hours=3")

    def taf(self, ids):
        return self.get_json(f"https://aviationweather.gov/api/data/taf?ids={','.join(ids)}&format=json")

    def openmeteo(self, lat, lon):
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
               "&hourly=cloud_cover,cloud_cover_low,precipitation_probability,precipitation,"
               "visibility,wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code"
               "&daily=sunrise,sunset&wind_speed_unit=kn&timezone=Asia%%2FTokyo&forecast_days=3") % (lat, lon)
        return self.get_json(url)

    def openmeteo_jma(self, lat, lon):
        """気象庁のメソモデル（MSM 5km・3時間ごと更新）→全球モデルへつなぐ時間別予報"""
        url = ("https://api.open-meteo.com/v1/jma?latitude=%.4f&longitude=%.4f"
               "&hourly=cloud_cover,cloud_cover_low,precipitation,wind_speed_10m,wind_direction_10m,weather_code"
               "&models=jma_seamless&wind_speed_unit=kn&timezone=Asia%%2FTokyo&forecast_days=3") % (lat, lon)
        return self.get_json(url)

    def jma_forecast(self, office):
        """気象庁の府県天気予報（6時間ごとの降水確率など。予報官が発表する公式の予報）"""
        return self.get_json(f"https://www.jma.go.jp/bosai/forecast/data/forecast/{office}.json")

    def page(self, url):
        return self.get_text(url, headers={"Accept": "text/html"}, timeout=40)

    def notify(self, server, topic, title, message, click=None, priority=3, tags=None, actions=None):
        body = {"topic": topic, "title": title, "message": message, "priority": priority,
                "tags": tags or ["airplane"]}
        if click:
            body["click"] = click
        if actions:
            body["actions"] = actions
        return self._req(server.rstrip("/") + "/", data=body, timeout=20)


class FixtureNet(Net):
    """オフライン試験用。fixtures/ の JSON/HTML を返す"""

    def __init__(self, cfg, root):
        super().__init__(cfg)
        self.root = root
        self.sent = []

    def _f(self, name, default=None):
        p = os.path.join(self.root, name)
        if not os.path.exists(p):
            if default is not None:
                return default
            raise FileNotFoundError(p)
        if p.endswith(".json"):
            return load_json(p, default)
        return open(p, encoding="utf-8").read()

    def area(self, lat, lon, r):
        return self._f("area.json", {"ac": []}).get("ac", [])

    def regs(self, regs):
        want = set(regs)
        return [a for a in self._f("regs.json", {"ac": []}).get("ac", []) if (a.get("r") or "").upper() in want]

    def types(self, types):
        want = set(types)
        return [a for a in self._f("types.json", {"ac": []}).get("ac", []) if (a.get("t") or "").upper() in want]

    def squawk(self, code):
        return [a for a in self._f("squawk.json", {"ac": []}).get("ac", []) if str(a.get("squawk")) == code]

    def reports(self, server, topic, since):
        return self._f("reports.ndjson", "") if not self._f("reports_done.json", {}).get(since) else ""

    def hexes(self, hexes):
        want = set(hexes)
        return [a for a in self._f("hexes.json", {"ac": []}).get("ac", []) if (a.get("hex") or "").lower() in want]

    def vrs_routes(self, airline):
        return self._f(f"vrs_{airline}.csv", "")

    def routeset(self, planes):
        if self._f("routeset_fail.json", {}).get("fail"):
            raise RuntimeError("JSONでない応答（試験）")
        known = self._f("routes.json", {})
        out = []
        for p in planes:
            r = known.get(p["callsign"])
            if r:
                codes = r.split("-")
                out.append({"callsign": p["callsign"], "airport_codes": r, "plausible": True,
                            "_airports": [{"icao": c} for c in codes]})
            else:
                out.append({"callsign": p["callsign"], "airport_codes": "unknown", "_airports": []})
        return out

    def haneda(self, flight_type, arrival_type, ymd):
        return self._f(f"haneda_{ymd}_{flight_type}{arrival_type}.json", [])

    def narita(self, dom_inter, dep_arr, date_iso):
        return self._f(f"narita_{date_iso}_{dom_inter}{dep_arr}.json", [])

    def metar(self, ids):
        return self._f("metar.json", [])

    def taf(self, ids):
        return self._f("taf.json", [])

    def openmeteo(self, lat, lon):
        return self._f("openmeteo.json", {})

    def openmeteo_jma(self, lat, lon):
        return self._f("openmeteo_jma.json", {})

    def jma_forecast(self, office):
        return self._f(f"jma_{office}.json", [])

    def page(self, url):
        return self._f("livery.html", "")

    def get_text(self, url, **kw):
        if "plane-alert" in url:
            return self._f("planealert.csv", "")
        return self._f("runways.csv", "")

    def notify(self, server, topic, title, message, click=None, priority=3, tags=None, actions=None):
        self.sent.append({"title": title, "message": message, "priority": priority, "actions": actions})
        print("NOTIFY", title, "|", message, "| ボタン:" + ",".join(a["label"] for a in actions or []))


# --------------------------------------------------------------------------
# 空港モデル
# --------------------------------------------------------------------------
def airport_ends(icao):
    """各滑走路端: {id, lat, lon, hdg(真方位), len_nm, far:(lat,lon)}"""
    ends = []
    for le, la1, lo1, he, la2, lo2, ft in RUNWAYS.get(icao, []):
        h1 = bearing(la1, lo1, la2, lo2)
        L = ft * 0.3048 / 1852.0
        ends.append({"id": le, "lat": la1, "lon": lo1, "hdg": h1, "len": L, "far": (la2, lo2)})
        ends.append({"id": he, "lat": la2, "lon": lo2, "hdg": (h1 + 180) % 360, "len": L, "far": (la1, lo1)})
    return ends


def iata_geo(code):
    """IATA または ICAO → (icao, lat, lon)"""
    if not code:
        return None
    aps = GEO["airports"]
    if code in aps:
        c = aps[code]
        return (c[0], c[1], c[2])
    for iata, c in aps.items():
        if c[0] == code:
            return (c[0], c[1], c[2])
    if code in AIRPORT_INFO:
        a = AIRPORT_INFO[code]
        return (code, a["lat"], a["lon"])
    return None


def ja_to_geo(name):
    iata = GEO["ja2iata"].get(name or "")
    return iata_geo(iata) if iata else None


def ap_label(code):
    """ICAO/IATA → 日本語の空港名（分かれば）"""
    if not code:
        return ""
    if code in AIRPORT_INFO:
        return AIRPORT_INFO[code]["name"]
    for ja, iata in GEO["ja2iata"].items():
        g = GEO["airports"].get(iata)
        if iata == code or (g and g[0] == code):
            return ja
    if code in MIL_FIELDS:
        return MIL_FIELDS[code][0]
    return code


# --------------------------------------------------------------------------
# 滑走路運用の予測
# --------------------------------------------------------------------------
HND_AXIS = 330.0  # 34L/34R の真方位（約）
NRT_AXIS = 330.0


def wind_config(icao, wdir, wspd, hour):
    """風 → 運用パターン。戻り値 (code, confidence)。code: N / S / S2 / X(不明) / 滑走路ID"""
    if wdir is None or wspd is None:
        return ("X", "未確定")
    if icao in ("RJTT", "RJAA"):
        u = wspd * math.cos(math.radians(wdir - (HND_AXIS if icao == "RJTT" else NRT_AXIS)))
        if abs(u) < 2.0 or wspd < 3:
            code, conf = ("N" if u >= 0 else "S"), "風弱く切替の可能性"
        else:
            code, conf = ("N" if u > 0 else "S"), ("見込み" if abs(u) >= 5 else "やや不確実")
        if icao == "RJTT" and code == "S" and 15 <= hour < 19:
            code = "S2"
        return (code, conf)
    # 汎用: 向かい風が最大の滑走路端
    best, bu = None, -99
    for e in airport_ends(icao):
        u = wspd * math.cos(math.radians(wdir - e["hdg"]))
        if u > bu:
            best, bu = e["id"], u
    return (best or "X", "見込み" if wspd >= 5 else "やや不確実")


CONFIG_LABEL = {
    "RJTT": {"N": "北風運用", "S": "南風運用", "S2": "南風運用（15〜19時の都心経路）", "X": "未確定"},
    "RJAA": {"N": "北風運用", "S": "南風運用", "X": "未確定"},
}
CONFIG_SHORT = {
    "RJTT": {"N": "北風", "S": "南風", "S2": "南風・都心経路", "X": "未確定"},
    "RJAA": {"N": "北風", "S": "南風", "X": "未確定"},
}
CONFIG_RUNWAYS = {
    "RJTT": {"N": ("34L/34R", "34R/05"), "S": ("22/23", "16L/16R"), "S2": ("16L/16R", "16R/22")},
    "RJAA": {"N": ("34R（大型機34L）", "34L"), "S": ("16L（大型機16R）", "16R")},
}


def predict_runway(icao, code, direction, other, actype, cfg):
    """1便ごとの使用滑走路の予測（文字列）"""
    if direction not in ("arr", "dep"):
        pair = CONFIG_RUNWAYS.get(icao, {}).get(code)
        return f"着{pair[0]}・発{pair[1]}" if pair else ("未確定" if code in ("X", None) else code)
    heavy = (actype or "") in set(cfg["heavy_types"])
    g = iata_geo(other) or ja_to_geo(other)
    ap = AIRPORT_INFO.get(icao)
    brg = bearing(ap["lat"], ap["lon"], g[1], g[2]) if (g and ap) else None
    if icao == "RJTT":
        if code == "N":
            if direction == "arr":
                return "34L/34R"
            if heavy:
                return "34R"
            if brg is None:
                return "34R/05"
            return "34R" if (brg >= 300 or brg < 120) else "05"
        if code == "S":
            if direction == "arr":
                return "22/23"
            if brg is None:
                return "16L/16R"
            return "16R" if 180 <= brg < 300 else "16L"
        if code == "S2":
            if direction == "arr":
                return "16L/16R"
            if brg is None:
                return "16R/22"
            return "22" if (180 <= brg < 330 and not heavy) else "16R"
        return "未確定"
    if icao == "RJAA":
        if code == "N":
            return ("34L" if heavy else "34R") if direction == "arr" else "34L"
        if code == "S":
            return ("16R" if heavy else "16L") if direction == "arr" else "16R"
        return "未確定"
    return code if code not in ("X", None) else "未確定"


# --------------------------------------------------------------------------
# 気象
# --------------------------------------------------------------------------
def parse_weather(net, cfg, state, now_utc, tz):
    """METAR/TAF（毎回）と Open-Meteo（毎時）。戻り値 weather[icao]"""
    aps = cfg["airports"]
    wx = state.setdefault("wx", {})
    try:
        for m in net.metar(aps):
            ic = m.get("icaoId")
            if ic in aps:
                prev = wx.setdefault(ic, {}).get("metar")
                if not prev or (m.get("obsTime") or 0) >= (prev.get("obsTime") or 0):
                    wx[ic]["metar"] = {k: m.get(k) for k in ("obsTime", "reportTime", "wdir", "wspd", "wgst",
                                                             "visib", "rawOb", "cover", "temp", "fltCat",
                                                             "wxString", "clouds")}
    except Exception as e:  # noqa: BLE001
        LOG.err("METAR", e)
    try:
        for t in net.taf(aps):
            ic = t.get("icaoId")
            if ic in aps:
                wx.setdefault(ic, {})["taf"] = {"raw": t.get("rawTAF"), "issue": t.get("issueTime"),
                                                "from": t.get("validTimeFrom"), "to": t.get("validTimeTo"),
                                                "fcsts": t.get("fcsts") or []}
    except Exception as e:  # noqa: BLE001
        LOG.err("TAF", e)
    for ic in aps:
        info = airport_latlon(ic, state)
        w = wx.setdefault(ic, {})
        age = now_utc.timestamp() - (w.get("om_at") or 0)
        if age < 3300 and w.get("om"):
            continue
        try:
            om = net.openmeteo(info[0], info[1])
            if om.get("hourly"):
                w["om"] = {"hourly": om["hourly"], "daily": om.get("daily", {})}
                w["om_at"] = now_utc.timestamp()
        except Exception as e:  # noqa: BLE001
            LOG.err(f"Open-Meteo {ic}", e)
        try:
            mj = net.openmeteo_jma(info[0], info[1])
            if mj.get("hourly"):
                w["msm"] = {"hourly": mj["hourly"]}
        except Exception as e:  # noqa: BLE001
            LOG.err(f"気象庁モデル（Open-Meteo） {ic}", e)
        if ic in JMA_AREA:
            try:
                w["jma"] = jma_pops(net.jma_forecast(JMA_AREA[ic][0]), JMA_AREA[ic][1])
            except Exception as e:  # noqa: BLE001
                LOG.err(f"気象庁の天気予報 {ic}", e)
    return wx


def jma_pops(doc, area):
    """気象庁の府県天気予報 → [[開始時刻(ISO), 降水確率%], ...]（6時間ごと）と天気の文章"""
    out = {"pops": [], "weathers": []}
    for block in (doc or [])[:1]:
        for ts in block.get("timeSeries") or []:
            times = ts.get("timeDefines") or []
            for a in ts.get("areas") or []:
                if (a.get("area") or {}).get("code") != area:
                    continue
                if a.get("pops"):
                    out["pops"] = [[t, int(p)] for t, p in zip(times, a["pops"]) if str(p).strip().isdigit()]
                if a.get("weathers"):
                    out["weathers"] = [[t, w] for t, w in zip(times, a["weathers"])]
        out["report"] = block.get("reportDatetime")
    return out


def jma_pop_at(jma, ts):
    """その時刻を含む6時間ブロックの降水確率（気象庁）"""
    best = None
    for t, p in (jma or {}).get("pops", []):
        try:
            t0 = dt.datetime.fromisoformat(t).timestamp()
        except ValueError:
            continue
        if t0 <= ts < t0 + 6 * 3600:
            best = p
    return best


def _vis_m(v):
    """aviationweather の視程（SM、'6+' など）→ m"""
    if v is None:
        return None
    s = str(v).strip()
    if s.endswith("+"):
        return 10000
    try:
        return min(10000, round(float(s) * 1609))
    except ValueError:
        return None


def _ceiling(clouds):
    """BKN/OVC の最も低い雲底（ft）"""
    bases = [c.get("base") for c in (clouds or []) if c.get("cover") in ("BKN", "OVC", "OVX") and c.get("base") is not None]
    return min(bases) if bases else None


PRECIP_RE = re.compile(r"(RA|SN|DZ|GR|GS|PL|SG|TS|SH|UP)")
WX_JA = [("TSRA", "雷雨"), ("SHRA", "にわか雨"), ("SHSN", "にわか雪"), ("FZRA", "着氷性の雨"), ("TS", "雷"),
         ("RA", "雨"), ("DZ", "霧雨"), ("SN", "雪"), ("GR", "ひょう"), ("FG", "霧"), ("BR", "もや"), ("HZ", "煙霧"),
         ("VCSH", "付近でにわか雨"), ("SQ", "スコール")]


def wx_ja(s):
    """'-SHRA BR' → '弱いにわか雨・もや'"""
    out = []
    for tok in (s or "").split():
        pre = "弱い" if tok.startswith("-") else "強い" if tok.startswith("+") else ""
        core = tok.lstrip("+-")
        name = next((ja for code, ja in WX_JA if core == code), None)
        if name is None:
            name = "".join(ja for code, ja in WX_JA if code in core and len(code) == 2 and code not in ("SH", "TS"))[:8] or core
        out.append(pre + name)
    return "・".join(out)


def taf_state_at(taf, ts):
    """TAF（デコード済）の時刻tsにおける卓越状態（BECMG は引き継ぎ）と一時的な変化（TEMPO/PROB）"""
    base = {"wdir": None, "wspd": None, "wx": None, "vis": None, "ceil": None}
    tempo = []
    if not taf or not taf.get("fcsts"):
        return None, tempo
    found = False
    for f in taf["fcsts"]:
        ch = f.get("fcstChange")
        t0, t1 = f.get("timeFrom") or 0, f.get("timeTo") or 0
        if ch in ("TEMPO", "PROB") or (f.get("probability") not in (None, 0)):
            if t0 <= ts < t1:
                tempo.append({"wx": f.get("wxString"), "vis": _vis_m(f.get("visib")), "ceil": _ceiling(f.get("clouds")),
                              "kind": "一時" if ch == "TEMPO" else "所により"})
            continue
        start = f.get("timeBec") or t0 if ch == "BECMG" else t0
        if ch == "FM" or ch is None:
            if t0 > ts:
                continue
            found = True
            if ch == "FM":
                base = {"wdir": None, "wspd": None, "wx": None, "vis": None, "ceil": None}
        elif ch == "BECMG":
            if ts < start:
                continue
            found = True
        wd = f.get("wdir")
        if f.get("wspd") is not None:
            base["wdir"] = None if isinstance(wd, str) else num(wd)
            base["wspd"] = num(f.get("wspd"))
        if f.get("wxString") is not None:
            base["wx"] = f.get("wxString")
        if f.get("visib") is not None:
            base["vis"] = _vis_m(f.get("visib"))
        if f.get("clouds"):
            base["ceil"] = _ceiling(f.get("clouds"))
    if not found or not ((taf.get("from") or 0) <= ts < (taf.get("to") or 0)):
        return None, tempo
    return base, tempo


def shoot_score(pop, precip, low, vis, base, tempo, metar_now):
    """撮影条件 ◎/○/△ と、その理由（どの予報で決まったか）"""
    why = []
    poor = fair = False
    if metar_now:
        if metar_now.get("wx") and PRECIP_RE.search(metar_now["wx"]):
            poor = True
            why.append(f"実況 {wx_ja(metar_now['wx'])}")
        if metar_now.get("vis") is not None and metar_now["vis"] < 3000:
            poor = True
            why.append(f"実況 視程{metar_now['vis']}m")
    if pop is not None and pop >= 60:
        poor = True
        why.append(f"降水確率{pop}%")
    elif pop is not None and pop >= 30:
        fair = True
        why.append(f"降水確率{pop}%")
    if precip is not None and precip >= 1.0:
        poor = True
        why.append(f"雨{precip:.0f}mm/h")
    if base:
        if base.get("wx") and PRECIP_RE.search(base["wx"]):
            poor = True
            why.append(f"TAF {wx_ja(base['wx'])}")
        if base.get("vis") is not None and base["vis"] < 3000:
            poor = True
            why.append(f"TAF 視程{base['vis']}m")
        elif base.get("vis") is not None and base["vis"] < 8000:
            fair = True
        if base.get("ceil") is not None and base["ceil"] < 1000:
            poor = True
            why.append(f"TAF 雲底{base['ceil']}ft")
        elif base.get("ceil") is not None and base["ceil"] < 3000:
            fair = True
            why.append(f"TAF 雲底{base['ceil']}ft")
    for tp in tempo or []:
        if tp.get("wx"):
            fair = True
            why.append(f"TAF {tp['kind']}{wx_ja(tp['wx'])}")
    if low is not None and low >= 50:
        fair = True
        why.append(f"低い雲{low:.0f}%")
    if vis is not None and not base and vis < 3000:
        poor = True
        why.append(f"視程{vis / 1000:.0f}km")
    if pop is None and precip is None and low is None and not base:
        return "?", ""
    score = "poor" if poor else "fair" if fair else "good"
    return score, "・".join(dict.fromkeys(why))


def hourly_table(icao, wx, day, tz, live_cfg, now_utc, start_code=None):
    """指定日の 0〜23時: 風・運用予測・撮影条件。
    風: いま=METAR（実況）→ TAF（空港の航空予報）→ 気象庁MSM → 汎用モデル
    運用: 直近2時間は実際の運用。その先は今の運用から出発し、追い風が目安を超える時間に切り替わるとみなす
    撮影条件: 降水確率=気象庁の予報、雲・雨量=気象庁MSM、視程・雲底・一時的な雨=TAF、いま=METAR"""
    w = wx.get(icao, {})
    om = (w.get("om") or {}).get("hourly") or {}
    msm = (w.get("msm") or {}).get("hourly") or {}
    idx = {tt: i for i, tt in enumerate(om.get("time", []))}
    idx2 = {tt: i for i, tt in enumerate(msm.get("time", []))}
    metar = w.get("metar") or {}
    nowts = now_utc.timestamp()
    metar_fresh = metar.get("obsTime") and nowts - metar["obsTime"] < 5400
    rows = []
    prev_code = start_code
    for h in range(24):
        local = dt.datetime.combine(day, dt.time(h, 0), tz)
        ts = int(local.timestamp())
        key = local.strftime("%Y-%m-%dT%H:00")
        i, j = idx.get(key), idx2.get(key)

        def g(name, src=om, k=i):
            arr = src.get(name) or []
            return arr[k] if (k is not None and k < len(arr)) else None

        is_now = ts <= nowts < ts + 3600
        base, tempo = taf_state_at(w.get("taf"), ts + 1800)
        # 風
        if is_now and metar_fresh and metar.get("wspd") is not None:
            wd = metar.get("wdir")
            wdir, wspd, src = (None if isinstance(wd, str) else num(wd)), num(metar.get("wspd")), "METAR"
        elif base and base.get("wspd") is not None:
            wdir, wspd, src = base["wdir"], base["wspd"], "TAF"
        elif g("wind_speed_10m", msm, j) is not None:
            wdir, wspd, src = g("wind_direction_10m", msm, j), g("wind_speed_10m", msm, j), "気象庁MSM"
        else:
            wdir, wspd, src = g("wind_direction_10m"), g("wind_speed_10m"), "予報"
        # 運用
        code, conf = wind_config(icao, wdir, wspd, h)
        if icao in ("RJTT", "RJAA") and prev_code in ("N", "S", "S2") and wspd is not None:
            u = wspd * math.cos(math.radians((wdir or 0) - (HND_AXIS if icao == "RJTT" else NRT_AXIS))) if wdir is not None else 0
            cur = "N" if prev_code == "N" else "S"
            tail = -u if cur == "N" else u
            if tail > TAILWIND_SWITCH_KT:
                cur = "S" if cur == "N" else "N"
                conf = "見込み"
            else:
                conf = "見込み" if abs(u) >= 5 else "風弱く切替の可能性"
            code = "S2" if (icao == "RJTT" and cur == "S" and 15 <= h < 19) else cur
        if live_cfg and live_cfg.get("code") and ts - nowts < 7200 and ts + 3600 > nowts:
            lc = live_cfg["code"]
            if icao == "RJTT" and lc in ("S", "S2"):
                lc = "S2" if 15 <= h < 19 else "S"
            code, conf, src = lc, "実運用", "実測"
        if code == "X" and prev_code:
            code, conf = prev_code, "未確定"
        prev_code = code
        # 撮影条件
        pop = jma_pop_at(w.get("jma"), ts + 1800)
        pop_src = "気象庁"
        if pop is None:
            pop, pop_src = g("precipitation_probability"), "予報"
        low = g("cloud_cover_low", msm, j)
        cc = g("cloud_cover", msm, j)
        pr = g("precipitation", msm, j)
        if low is None:
            low, cc, pr = g("cloud_cover_low"), g("cloud_cover"), g("precipitation")
        vis = g("visibility")
        mnow = None
        if is_now and metar_fresh:
            mnow = {"wx": metar.get("wxString"), "vis": _vis_m(metar.get("visib"))}
        score, why = shoot_score(pop, pr, low, vis, base, tempo, mnow)
        rows.append({"h": h, "wdir": None if wdir is None else round(wdir), "wspd": None if wspd is None else round(wspd),
                     "gust": g("wind_gusts_10m"), "wsrc": src, "cfg": code, "cfgConf": conf,
                     "cloud": cc, "cloudLow": low, "pop": pop, "popSrc": pop_src, "precip": pr, "vis": vis,
                     "code": g("weather_code"), "tempo": " ".join(tp["wx"] for tp in tempo if tp.get("wx")),
                     "shoot": score, "shootWhy": why})
    daily = (w.get("om") or {}).get("daily") or {}
    sun = {}
    for i, tday in enumerate(daily.get("time", [])):
        if tday == day.isoformat():
            sun = {"rise": (daily.get("sunrise") or [None])[i], "set": (daily.get("sunset") or [None])[i]}
    return rows, sun


# --------------------------------------------------------------------------
# 羽田 時刻表
# --------------------------------------------------------------------------
def fetch_timetable(net, cfg, state, now_local, tz, data_dir):
    """今日・明日の時刻表（羽田・成田の公式サイト）。30分に1回更新。空港ごとに失敗しても前回分で続ける"""
    path = os.path.join(data_dir, "timetable.json")
    tt = load_json(path, {})
    days = [now_local.date(), now_local.date() + dt.timedelta(days=1)]
    day_isos = [d.isoformat() for d in days]
    want = [ic for ic in cfg["airports"] if AIRPORT_INFO.get(ic, {}).get("timetable")]
    if not want:
        return tt
    if now_local.timestamp() - (tt.get("_at") or 0) <= 1700 and tt.get("_days") == day_isos \
            and all(ic in tt for ic in want):
        return tt
    out = {"_at": now_local.timestamp(), "_days": day_isos}
    for ic in want:
        kind = AIRPORT_INFO[ic]["timetable"]
        got, ok = {}, 0
        for d in days:
            rows = []
            if kind == "haneda":
                ymd = d.strftime("%Y%m%d")
                for ftype, atype, region, direction in ((1, 1, "dom", "dep"), (1, 2, "dom", "arr"),
                                                        (2, 1, "int", "dep"), (2, 2, "int", "arr")):
                    try:
                        for rec in net.haneda(ftype, atype, ymd):
                            r = haneda_row(rec, region, direction)
                            if r:
                                rows.append(r)
                        ok += 1
                    except Exception as e:  # noqa: BLE001
                        LOG.err(f"羽田時刻表 {ymd} {region}-{direction}", e)
            elif kind == "narita":
                for di, da, region, direction in (("I", "A", "int", "arr"), ("I", "D", "int", "dep"),
                                                  ("D", "A", "dom", "arr"), ("D", "D", "dom", "dep")):
                    try:
                        for rec in net.narita(di, da, d.isoformat()):
                            r = narita_row(rec, region, direction)
                            if r:
                                rows.append(r)
                        ok += 1
                    except Exception as e:  # noqa: BLE001
                        LOG.err(f"成田時刻表 {d.isoformat()} {region}-{direction}", e)
            got[d.isoformat()] = rows
        if ok == 0 and tt.get(ic):
            LOG.note(f"{AIRPORT_INFO[ic]['name']}時刻表: 取得失敗のため前回分を使用")
            got = {k: v for k, v in tt[ic].items() if k in day_isos}
        out[ic] = got
    save_json(path, out)
    # 航空会社 ICAO→IATA 対応を学習
    amap = state.setdefault("airline_map", {})
    for ic in want:
        for rows in out.get(ic, {}).values():
            for r in rows:
                m = re.match(r"^([A-Z0-9]{2})\d", r["fl"])
                if r.get("al") and m and len(r["al"]) == 3:
                    amap[r["al"]] = m.group(1)
    return out


def narita_row(rec, region, direction):
    fl = fl_norm(rec.get("flightCode") or rec.get("displayFlightCode") or "")
    on = hhmm_ok(rec.get("scheduledTime"))
    if not fl or not on:
        return None
    chg = hhmm_ok(rec.get("changeScheduledTime"))
    ap = (rec.get("airport") or {})
    orig = ap.get("original") or next((v for v in ap.values() if isinstance(v, dict)), {}) or {}
    st = (rec.get("status") or {}).get("status", "") if isinstance(rec.get("status"), dict) else ""
    cat = ("arrived" if st in ("到着", "旅客降機") or "到着済" in st else "departed" if "出発済" in st
           else "cancelled" if "欠航" in st else "")
    return {"dir": direction, "region": region, "fl": fl,
            "al": ((rec.get("airline") or {}).get("3LetterCode") or ""),
            "cs": [fl_norm(c.get("codeShareNo", "")) for c in (rec.get("codeShare") or []) if c.get("codeShareNo")],
            "time": on, "rev": chg if chg and chg != on else "",
            "ap": orig.get("name") or orig.get("3LetterCode") or "", "apc": orig.get("3LetterCode") or "",
            "via": "", "term": rec.get("displayTerminal") or "", "status": "" if st in ("共同運航便", "定刻") else st,
            "cat": cat}


def row_code(r):
    """時刻表の相手空港 → IATA（成田は公式データにコードあり、羽田は都市名から）"""
    return r.get("apc") or GEO["ja2iata"].get(r.get("ap") or "")


def haneda_row(rec, region, direction):
    airlines = rec.get("airlines") or [{}]
    fl = fl_norm(airlines[0].get("flightNumber", ""))
    if not fl:
        return None
    st = rec.get("status") or {}
    term = (rec.get("terminal") or {}).get("terminal", "") if isinstance(rec.get("terminal"), dict) else ""
    on, chg = hhmm_ok(rec.get("on_time")), hhmm_ok(rec.get("change_time"))
    if not on:
        return None
    return {"dir": direction, "region": region, "fl": fl, "al": airlines[0].get("airline", "") or "",
            "cs": [fl_norm(a.get("flightNumber", "")) for a in airlines[1:] if a.get("flightNumber")],
            "time": on, "rev": chg if chg and chg != on else "", "ap": rec.get("area_name", "") or "",
            "via": rec.get("via_area_name", "") or "", "term": term,
            "status": (st.get("text") or "") if isinstance(st, dict) else "",
            "cat": (st.get("category") or "") if isinstance(st, dict) else ""}


def tt_index(tt):
    """{(icao, date, dir, fl): row}"""
    idx = {}
    for ic, days in tt.items():
        if ic.startswith("_"):
            continue
        for d, rows in days.items():
            for r in rows:
                idx[(ic, d, r["dir"], r["fl"])] = r
    return idx


def cs_to_fl(cs, state):
    """コールサイン 'ANA241' → 便名 'NH241'（学習済み対応表を使う）"""
    m = re.match(r"^([A-Z]{3})(\d{1,4})([A-Z]?)$", cs or "")
    if not m:
        return None
    iata = state.get("airline_map", {}).get(m.group(1)) or SEED_AIRLINES.get(m.group(1))
    if not iata:
        return None
    return fl_norm(iata + m.group(2) + m.group(3))


SEED_AIRLINES = {
    "ANA": "NH", "JAL": "JL", "SKY": "BC", "SFJ": "7G", "SNJ": "6J", "ADO": "HD", "APJ": "MM",
    "JJP": "GK", "JTA": "NU", "NCA": "KZ", "SJO": "IJ", "IBX": "FW", "FDA": "JH", "UAL": "UA",
    "DAL": "DL", "AAL": "AA", "HAL": "HA", "ACA": "AC", "KAL": "KE", "AAR": "OZ", "CCA": "CA",
    "CES": "MU", "CSN": "CZ", "CAL": "CI", "EVA": "BR", "SIA": "SQ", "THA": "TG", "CPA": "CX",
    "HVN": "VN", "PAL": "PR", "CEB": "5J", "GIA": "GA", "MAS": "MH", "QFA": "QF", "ANZ": "NZ",
    "AFR": "AF", "BAW": "BA", "DLH": "LH", "KLM": "KL", "SWR": "LX", "FIN": "AY", "SAS": "SK",
    "UAE": "EK", "QTR": "QR", "ETD": "EY", "THY": "TK", "AIC": "AI", "VJC": "VJ", "TGW": "TR",
    "FDX": "FX", "UPS": "5X", "GTI": "5Y", "CKS": "K4", "HKE": "UO", "JJA": "7C", "TWB": "TW",
    "JNA": "LJ", "ESR": "ZE", "AMU": "NX", "CRK": "HX", "ETH": "ET", "LOT": "LO", "AUA": "OS",
}


# --------------------------------------------------------------------------
# 特別塗装機 台帳（自動で追加・抹消）
# --------------------------------------------------------------------------
SEED_REGISTRY = [
    ("JA461A", "ANA", "ANA future promise 3"), ("JA58AN", "ANA", "ふるさとJET"),
    ("JA614A", "ANA", "スターアライアンス塗装"), ("JA784A", "ANA", "イーブイジェットNH"),
    ("JA819A", "ANA", "ポケモンジェット 赤"), ("JA923A", "ANA", "ポケモンジェット 緑"),
    ("JA56AN", "ANA", "ポケモンジェット 青"), ("JA871A", "ANA", "ANA future promise Jet 1"),
    ("JA872A", "ANA", "スターアライアンス塗装"), ("JA874A", "ANA", "ANA future promise Jet 2"),
    ("JA875A", "ANA", "スターアライアンス塗装"), ("JA880A", "ANA", "スターアライアンス塗装"),
    ("JA882A", "ANA", "B787 特別塗装"), ("JA894A", "ANA", "ピカチュウジェットNH"),
    ("JA01XJ", "JAL", "A350「挑戦」レッド"), ("JA02XJ", "JAL", "A350「革新」シルバー"),
    ("JA03XJ", "JAL", "A350「エコ」グリーン"), ("JA15XJ", "JAL", "ワンワールド塗装"),
    ("JA245J", "JAL", "USJジェット 2"), ("JA339J", "JAL", "JAL Jubilee Express"),
    ("JA861J", "JAL", "ワンワールド塗装"), ("JA868J", "JAL", "CONTRAIL"),
    ("JA869J", "JAL", "ワンワールド塗装"), ("JA05RK", "JTA", "ジンベエジェット"),
    ("JA06RK", "JTA", "さくらジンベエ"), ("JA07RK", "JTA", "ゆいまーるジンベエ"),
    ("JA10RK", "JTA", "世界自然遺産"), ("JA803X", "ソラシドエア", "なっしージェット"),
    ("JA607A", "AIRDO", "ロコンジェット"), ("JA73AB", "スカイマーク", "ピカチュウジェットBC1"),
    ("JA73NG", "スカイマーク", "ピカチュウジェットBC2"), ("JA823P", "Peach", "特別塗装"),
    ("JA81YA", "スプリング・ジャパン", "ヤマト運輸 1"), ("JA82YA", "スプリング・ジャパン", "ヤマト運輸 2"),
    ("JA83YA", "スプリング・ジャパン", "ヤマト運輸 3"),
]
REG_RE = r"(?:JA[0-9]{2}[0-9A-Z]{1,3}|JA[0-9]{4}|80-11[0-9]{2}|[A-Z0-9]{1,2}-[A-Z0-9]{3,5}|HL[0-9]{4}|N[0-9]{1,5}[A-Z]{0,2})"


REG_TOKEN = re.compile(r"(?<![A-Z0-9-])([A-Z0-9]{1,2}-[A-Z0-9]{2,5}|JA[0-9]{2}[0-9A-Z]{1,3}|N[0-9]{1,5}[A-Z]{0,2}|HL[0-9]{4}|80-11[0-9]{2})(?![A-Z0-9-])")


def _html_text(fragment):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", fragment)
    t = re.sub(r"(?i)<br\s*/?>|</(p|li|div|h[1-6]|tr|td|figcaption)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    for a_, b_ in (("&nbsp;", " "), ("&amp;", "&"), ("&#8217;", "'"), ("&#039;", "'"), ("&quot;", '"')):
        t = t.replace(a_, b_)
    return t


def parse_livery_page(html):
    """Flightradar24 機体ページへのリンク一覧から {reg: {name, type, airline}}。
    世界版はリンク内の記号にハイフンが無い（VT-ANP → vtanp）ので、本文の表記と突き合わせて直す"""
    text = _html_text(html)
    lines = [" ".join(x.split()) for x in text.split("\n") if x.strip()]
    tokens = {}
    for line in lines:
        for m in REG_TOKEN.finditer(line.upper()):
            tokens.setdefault(m.group(1).replace("-", ""), m.group(1))
    regs = {}
    airline = ""
    for m in re.finditer(r"(?is)<h[2-4][^>]*>(.*?)</h[2-4]>|flightradar24\.com/data/aircraft/([A-Za-z0-9\-]{3,10})", html):
        if m.group(1) is not None:
            airline = " ".join(_html_text(m.group(1)).split())[:40]
            continue
        slug = m.group(2).upper()
        reg = tokens.get(slug.replace("-", ""), slug)
        if not re.fullmatch(r"[A-Z0-9]{1,2}-?[A-Z0-9]{2,6}", reg) or reg.isdigit():
            continue
        regs.setdefault(reg, {"name": "", "type": "", "airline": airline})
    for line in lines:
        up = line.upper()
        for r, v in regs.items():
            if v["name"] or r not in up:
                continue
            rest = line[up.index(r) + len(r):].strip(" ：:-–")
            mt = re.match(r"^([^（(]{1,40}?)\s*[（(]([^)）]{1,40})[)）]", rest)
            if mt:
                v["name"], v["type"] = mt.group(1).strip(), mt.group(2).strip()
            elif rest:
                v["name"] = rest[:40]
    return regs


def maintain_registry(net, cfg, state, now_local, force=False):
    """1日1回（4時以降の最初の実行）台帳を更新"""
    reg = state.setdefault("registry", {})
    log = state.setdefault("changelog", [])
    today = now_local.date().isoformat()
    if not reg:
        for r, airline, name in SEED_REGISTRY:
            reg[r] = {"name": name, "airline": airline, "type": "", "source": "初期登録", "added": today,
                      "last_listed": today, "status": "active"}
        log.append({"date": today, "action": "add", "reg": "（初期登録）", "name": f"{len(SEED_REGISTRY)}機",
                    "reason": "初期台帳を登録"})
    meta = state.setdefault("registry_meta", {})
    if not force and (meta.get("checked") == today or now_local.hour < 4):
        return
    found_all, ok_sources, ok_urls = {}, 0, set()
    for url in cfg.get("livery_sources", []):
        try:
            found = parse_livery_page(net.page(url))
            prev = meta.get("count_" + url, 0)
            if len(found) < max(5, 0.5 * prev):
                LOG.note(f"台帳: {url} の件数が異常に少ない（{len(found)}件）ため今回は反映しない")
                continue
            meta["count_" + url] = len(found)
            ok_sources += 1
            ok_urls.add(url)
            for r, v in found.items():
                found_all.setdefault(r, dict(v, source=url))
        except Exception as e:  # noqa: BLE001
            LOG.err(f"台帳 {url}", e)
    meta["checked"] = today
    if ok_sources == 0:
        meta["fail_days"] = meta.get("fail_days", 0) + 1
        return
    meta["fail_days"] = 0
    meta["ok"] = today
    ignored = set(cfg.get("ignored_regs", []))
    for r, v in found_all.items():
        if r in ignored:
            continue
        e = reg.get(r)
        if not e or e.get("status") != "active":
            reg[r] = {"name": v.get("name") or (e or {}).get("name", ""),
                      "airline": v.get("airline") or (e or {}).get("airline", ""),
                      "type": v.get("type", ""), "source": v["source"], "added": today, "last_listed": today,
                      "status": "active"}
            log.append({"date": today, "action": "add", "reg": r, "name": reg[r]["name"],
                        "reason": "特別塗装機一覧に掲載"})
        else:
            e["last_listed"] = today
            if v.get("name"):
                e["name"] = v["name"]
            if v.get("type"):
                e["type"] = v["type"]
            if v.get("airline"):
                e["airline"] = v["airline"]
            e["source"] = v["source"]
    # その機体が載っていた情報源を正常に取れた日に、7回続けて載っていなければ抹消
    # （取得失敗の日・件数が異常な日は数えない）
    first_src = (cfg.get("livery_sources") or [""])[0]
    for r, e in reg.items():
        if e.get("status") != "active":
            continue
        if r in found_all:
            e["miss"] = 0
            continue
        src = e.get("source") if str(e.get("source", "")).startswith("http") else first_src
        if src not in ok_urls:
            continue
        e["miss"] = e.get("miss", 0) + 1
        if e["miss"] >= 7:
            e["status"] = "removed"
            e["removed"] = today
            log.append({"date": today, "action": "remove", "reg": r, "name": e.get("name", ""),
                        "reason": "一覧から7日以上消えている（塗装終了・退役とみなす）"})
    del log[:-200]


def watchlist(cfg, state):
    out = {}
    for r, e in state.get("registry", {}).items():
        if e.get("status") == "active" and r not in cfg.get("ignored_regs", []):
            out[r] = {"name": e.get("name") or "特別塗装機", "airline": e.get("airline", ""), "pinned": False}
    for r, name in (cfg.get("pinned_regs") or {}).items():
        out[r.upper()] = {"name": name, "airline": "", "pinned": True}
    return out


# --------------------------------------------------------------------------
# 政府・要人機・軍用機・珍しい機体（Plane-Alert-DB）
# --------------------------------------------------------------------------
PA_GROUPS = {
    "vip": ["Head of State", "Governments", "Royal Aircraft", "Radiohead", "Dictator Alert", "Quango"],
    "mil": ["USAF", "Other Air Forces", "United States Navy", "United States Marine Corps", "GAF", "RAF",
            "Other Navies", "Army Air Corps", "Royal Navy Fleet Air Arm", "Toy Soldiers", "Special Forces",
            "Gunship", "UAV", "Zoomies", "Oxcart", "Nuclear", "Aerobatic Teams"],
    "rare": ["Distinctive", "Historic", "Big Hello", "Gas Bags", "Da Comrade", "Ukraine"],
    # 既定では使わない（羽田の海保・国の業務機など毎日来る機体が多い）
    "public": ["Coastguard", "Police Forces", "UK National Police Air Service", "Aerial Firefighter",
               "Fire Fighting", "Flying Doctors", "CAP", "Dogs with Jobs", "Ptolemy would be proud"],
}
PA_LABEL = {"vip": "政府・要人機", "mil": "軍用機", "rare": "珍しい機体", "public": "公用機", "extra": "注目機"}
# 個人・企業の自家用機など、特定の人の移動を追うことになる分類は使わない
PA_PRIVATE = {"As Seen on TV", "Bizjets", "Climate Crisis", "Don't you know who I am?", "Oligarch",
              "Jesus he Knows me", "Football", "Vanity Plate", "Hired Gun", "Joe Cool", "PIA",
              "Jump Johnny Jump", "Perfectly Serviceable Aircraft", "Watch Me Fly", "You came here in that thing?"}


def pa_categories(cfg):
    """使う分類 → まとまり名"""
    pc = cfg.get("plane_alert") or {}
    out = {}
    for g in pc.get("groups") or []:
        for c in PA_GROUPS.get(g, []):
            out[c] = g
    for c in pc.get("extra_categories") or []:
        if isinstance(c, str):
            out.setdefault(c, next((g for g, cs in PA_GROUPS.items() if c in cs), "extra"))
    for c in pc.get("exclude_categories") or []:
        out.pop(c, None)
    return out


def load_plane_alert(net, cfg, state, data_dir, now_utc):
    """1日1回データベースを取得し、使う分類だけを小さな索引にして保存する。{hex: [reg, 運用者, 型式, 分類, まとまり]}"""
    pc = cfg.get("plane_alert") or {}
    path = os.path.join(data_dir, "planealert.json")
    meta = state.setdefault("pa_meta", {})
    if not pc.get("enabled", True):
        meta.update({"count": 0, "off": True})
        return {}
    cats = pa_categories(cfg)
    sig = "|".join(sorted(f"{c}={g}" for c, g in cats.items()))
    nowts = now_utc.timestamp()
    idx = load_json(path, None) if os.path.exists(path) else None
    fresh = idx is not None and meta.get("sig") == sig and nowts - meta.get("at", 0) < 20 * 3600
    if fresh:
        return idx
    try:
        import csv
        import io
        text = net.get_text(pc.get("url") or DEFAULT_CONFIG["plane_alert"]["url"], timeout=60)
        new = {}
        for r in csv.DictReader(io.StringIO(text)):
            cat = (r.get("Category") or "").strip()
            hx = (r.get("$ICAO") or "").strip().lower()
            op = (r.get("$Operator") or "").strip()
            if cat not in cats or cat in PA_PRIVATE or not re.match(r"^[0-9a-f]{6}$", hx):
                continue
            if op.lower() in ("private", "private owner", "") and cats[cat] != "mil":
                continue
            new[hx] = [(r.get("$Registration") or "").strip().upper(), op[:48],
                       (r.get("$ICAO Type") or "").strip().upper(), cat, cats[cat]]
        # 取得件数が前回の半分未満なら異常とみなして前回の索引を使う
        if idx and len(new) < max(50, len(idx) * 0.5) and meta.get("sig") == sig:
            LOG.note(f"政府・軍用機データベースの件数が少ない（{len(new)}件）ため前回分を使用")
            return idx
        save_json(path, new)
        meta.update({"at": nowts, "sig": sig, "count": len(new),
                     "date": dt.datetime.fromtimestamp(nowts, dt.timezone.utc).date().isoformat()})
        meta.pop("off", None)
        return new
    except Exception as e:  # noqa: BLE001
        LOG.err("政府・軍用機データベースの取得", e)
        return idx or {}


def pa_regulars(cfg, state, today):
    """直近14日で何日も来ている機体（羽田の海保機・定期の軍用連絡機など）は珍しくないので外す"""
    need = int((cfg.get("plane_alert") or {}).get("regular_days") or 0)
    if need <= 0:
        return set()
    since = (today - dt.timedelta(days=14)).isoformat()
    return {hx for hx, ds in state.get("pa_seen", {}).items() if len([d for d in ds if d >= since]) >= need}


# --------------------------------------------------------------------------
# ADS-B の処理
# --------------------------------------------------------------------------
def ac_norm(a):
    alt = a.get("alt_baro")
    ground = alt == "ground"
    return {"hex": (a.get("hex") or "").lower().lstrip("~"), "cs": clean_cs(a.get("flight")),
            "reg": (a.get("r") or "").upper(), "type": (a.get("t") or "").upper(),
            "desc": a.get("desc") or "", "lat": num(a.get("lat")), "lon": num(a.get("lon")),
            "alt": 0.0 if ground else num(alt), "ground": ground, "gs": num(a.get("gs")),
            "trk": num(a.get("track")), "vr": num(a.get("baro_rate") if a.get("baro_rate") is not None else a.get("geom_rate")),
            "flags": int(a.get("dbFlags") or 0), "seen": num(a.get("seen_pos")) or 0,
            "sqk": str(a.get("squawk") or ""), "emergency": str(a.get("emergency") or "")}


EMG_SQUAWK = {"7700": "緊急事態（7700）", "7600": "無線故障（7600）", "7500": "ハイジャック（7500）"}
EMG_FIELD = {"general": "緊急事態", "nordo": "無線故障", "unlawful": "ハイジャック", "minfuel": "燃料不足",
             "downed": "緊急事態"}


def emg_label(a):
    if a.get("sqk") in EMG_SQUAWK:
        return EMG_SQUAWK[a["sqk"]]
    return EMG_FIELD.get(a.get("emergency") or "")


def classify(a, watch, cfg, pa=None, regulars=()):
    if a["reg"] and a["reg"] in watch:
        return "watch", watch[a["reg"]]["name"]
    p = (pa or {}).get(a["hex"])
    if p and a["hex"] not in regulars:
        g = p[4]
        label = PA_LABEL.get(g, "注目機")
        op = p[1] if p[1] and p[1].lower() != "private" else p[3]
        return ("type" if g == "rare" else "mil"), f"{label}（{op}）"
    if a["type"] in LIGHT_TYPES:
        return None, None
    if (a["flags"] & 1) or a["type"] in MIL_TYPES:
        return "mil", "軍用・政府機"
    if a["type"] and a["type"] in set(cfg["rare_types"]):
        return "type", "希少機種 " + a["type"]
    m = re.match(r"^([A-Z]{3})\d{1,4}[A-Z]?$", a["cs"])
    if m and m.group(1) not in set(cfg["common_operators"]):
        return "op", "珍しい運航者 " + m.group(1)
    return None, None


def detect_runway(icao, acs):
    """最終進入・離陸直後の機体の向きから、いま使っている滑走路を数える"""
    ends = airport_ends(icao)
    arr, dep = {}, {}
    for a in acs:
        if a["ground"] or a["lat"] is None or a["alt"] is None or a["trk"] is None:
            continue
        best = None
        for e in ends:
            x, y = local_xy(e["lat"], e["lon"], a["lat"], a["lon"])
            h = math.radians(e["hdg"])
            along = x * math.sin(h) + y * math.cos(h)
            cross = x * math.cos(h) - y * math.sin(h)
            if ang_diff(a["trk"], e["hdg"]) > 22:
                continue
            if -12 <= along <= 0.3 and a["alt"] < 4500 and abs(cross) < 0.6 + 0.04 * abs(along) and (a["vr"] is None or a["vr"] <= 300):
                cand = ("arr", e["id"], abs(cross))
            elif e["len"] <= along <= e["len"] + 7 and a["alt"] < 1800 + 700 * (along - e["len"]) and abs(cross) < 1.0 and (a["vr"] is None or a["vr"] >= 0):
                cand = ("dep", e["id"], abs(cross))
            else:
                continue
            if not best or cand[2] < best[2]:
                best = cand
        if best:
            (arr if best[0] == "arr" else dep)[best[1]] = (arr if best[0] == "arr" else dep).get(best[1], 0) + 1
    return arr, dep


def config_from_votes(icao, arr, dep):
    votes = list(arr) + list(dep)
    if not votes:
        return None
    if icao == "RJTT":
        if any(r in ("16L", "16R") for r in arr):
            return "S2"
        if any(r in ("22", "23") for r in arr) or any(r in ("16L", "16R") for r in dep):
            return "S"
        if any(r in ("34L", "34R") for r in votes) or "05" in dep:
            return "N"
        return None
    if icao == "RJAA":
        if any(r.startswith("34") for r in votes):
            return "N"
        if any(r.startswith("16") for r in votes):
            return "S"
        return None
    return max(votes, key=lambda r: arr.get(r, 0) + dep.get(r, 0))


def airport_latlon(icao, state):
    if icao in AIRPORT_INFO:
        return (AIRPORT_INFO[icao]["lat"], AIRPORT_INFO[icao]["lon"])
    g = iata_geo(icao)
    if g:
        return (g[1], g[2])
    extra = state.get("extra_airports", {}).get(icao)
    if extra:
        return (extra["lat"], extra["lon"])
    raise KeyError(f"空港 {icao} の座標が分かりません")


NOTABLE = ("watch", "type", "mil", "emg")


def in_japan(lat, lon):
    return 20 <= lat <= 46.5 and 122 <= lon <= 154


def domestic_candidate_types(cfg):
    s = {str(x).upper() for x in (cfg.get("domestic_unusual_types") or [])}
    for lst in (cfg.get("airport_unusual_types") or {}).values():
        s |= {str(x).upper() for x in lst or []}
    return s


def jp_airport(code):
    return bool(code) and code[:2] in ("RJ", "RO")


def domestic_unusual(a, cfg, aps):
    """国内線（両端が日本の空港）で撮影拠点に発着する便に、ふつう入らない機材が入っていれば説明文を返す"""
    route = a.get("route") or []
    if len(route) < 2 or not a.get("type"):
        return None
    orig, dest = route[0], route[-1]
    touch = [ic for ic in aps if ic in (orig, dest)]
    if not touch:
        return None
    t = a["type"]
    if jp_airport(orig) and jp_airport(dest) and t in {x.upper() for x in cfg.get("domestic_unusual_types") or []}:
        return f"国内線では珍しい機材 {t}"
    for ic in touch:
        if t in {x.upper() for x in (cfg.get("airport_unusual_types") or {}).get(ic, [])}:
            return f"{AIRPORT_INFO.get(ic, {}).get('name', ic)}には珍しい機材 {t}"
    return None
# 違いが小さく、撮影上は同じ扱いでよい型式のまとまり（787-8/-9/-10、737NG/MAX、A320/321/319 の ceo/neo）
TYPE_FAMILY = [{"B788", "B789", "B78X"}, {"B737", "B738", "B739", "B38M", "B39M"},
               {"A320", "A20N"}, {"A321", "A21N"}, {"A319", "A19N"}]


def same_family(a, b):
    return a == b or any(a in f and b in f for f in TYPE_FAMILY)


def unusual_equipment(a, aps, ttx, equip, today):
    """時刻表で撮影拠点に発着する国際線に、過去の実績に無い型式が入っていれば説明文を返す。
    実績が3回以上・2日以上あるときだけ判定する（学習が浅いうちは言わない）"""
    for ic in aps:
        for d in ("arr", "dep"):
            row = ttx.get((ic, today, d, a["fl"]))
            if not row or row.get("region") == "dom":
                continue  # 国内線は機種の一覧（domestic_unusual_types など）だけで判定する
            hist = equip.get(f"{ic}|{d}|{a['fl']}") or []
            if len(hist) < 3 or len({x[0] for x in hist}) < 2:
                continue
            types = [x[1] for x in hist if x[1]]
            if types and not any(same_family(a["type"], x) for x in types):
                usual = max(set(types), key=types.count)
                return f"普段と違う機材 {a['type']}（{a['fl']} はいつも {usual}）"
    return None


def returns_expected(reg, sev, orig, dest, aps):
    """その区間の先から撮影拠点へ戻ってくると見込めるか。
    ・日本の航空会社の機体（JA）が撮影拠点から出た区間: 行き先で折り返して戻る（例: JL6でJFK→翌日JL5）
    ・日本の特別塗装機が国内の空港どうしを飛んでいる区間: 国内線網の中で羽田・成田に戻ってくる
    ・外国の航空会社の機体は対象外（本拠地に帰った後は世界中どこへ飛ぶか分からない。
      例: エミレーツのA380は成田→ドバイの後、日本に戻るとは限らない）
    ・撮影拠点に関係のない区間（例: カンタスのロサンゼルス→シドニー）も対象外"""
    reg = (reg or "").upper()
    japanese = reg.startswith("JA") or bool(re.match(r"^\d{2}-\d{4}$", reg))
    if not japanese or not dest or dest in aps:
        return False
    if orig in aps:
        return True
    return sev == "watch" and dest[:2] in ("RJ", "RO") and (orig or "RJ")[:2] in ("RJ", "RO")
SHIP_DAYS = 28


def learn_leg(state, a, nowts, tz):
    """機材繰り（シップパターン）の学習。同じ機体が続けて飛んだ便を「便A→便B」として数える。
    あわせて「ある航空会社の機体が空港Xに着いた後、次にどこへ飛んだか」も数える"""
    fl = a.get("fl") or a.get("cs")
    if not fl or not re.match(r"^[A-Z]{3}\d", a.get("cs") or ""):
        return
    ship = state.setdefault("ship", {"last": {}, "fl": {}, "flout": {}, "nd": {}})
    date = dt.datetime.fromtimestamp(nowts, tz).date().isoformat()
    route = a.get("route") or []
    orig, dest = (route[0], route[-1]) if len(route) >= 2 else (None, None)
    last = ship["last"].get(a["hex"])
    if last and last["fl"] == fl and nowts - last["t"] < 20 * 3600:
        last["t"] = nowts
        if dest and not last.get("dest"):
            last["orig"], last["dest"] = orig, dest
        return
    if last and nowts - last["t"] < 20 * 3600:
        # 前の便の到着地から今の便が出ている（途中の便を見落としていない）ときだけ数える
        if orig is None or last.get("dest") is None or orig == last["dest"]:
            _add_date(ship["fl"].setdefault(f"{last['fl']}>{fl}", []), date)
            _add_date(ship["flout"].setdefault(last["fl"], []), date)
        if last.get("dest") and dest and orig == last["dest"]:
            nd = ship["nd"].setdefault(f"{a['cs'][:3]}|{last['dest']}", {})
            _add_date(nd.setdefault(dest, []), date + "|" + a["hex"])
    ship["last"][a["hex"]] = {"fl": fl, "t": nowts, "orig": orig, "dest": dest}


def _add_date(lst, d):
    if d not in lst:
        lst.append(d)
        del lst[:-12]


def prune_ship(state, today):
    ship = state.get("ship")
    if not ship:
        return
    cut = (today - dt.timedelta(days=SHIP_DAYS)).isoformat()
    for tbl in (ship["fl"], ship["flout"]):
        for k in list(tbl):
            tbl[k] = [d for d in tbl[k] if d[:10] >= cut]
            if not tbl[k]:
                del tbl[k]
    for k in list(ship["nd"]):
        for d in list(ship["nd"][k]):
            ship["nd"][k][d] = [x for x in ship["nd"][k][d] if x[:10] >= cut]
            if not ship["nd"][k][d]:
                del ship["nd"][k][d]
        if not ship["nd"][k]:
            del ship["nd"][k]
    for h in [h for h, v in ship["last"].items() if v["t"] < (dt.datetime.combine(today, dt.time()) - dt.timedelta(days=3)).timestamp()]:
        del ship["last"][h]


def ship_next(ship, fl):
    """便flの次に同じ機体が入る便の候補 [(便, 回数, 全体)]（多い順）"""
    total = len((ship or {}).get("flout", {}).get(fl, []))
    if not total:
        return [], 0
    out = []
    pre = fl + ">"
    for k, v in ship.get("fl", {}).items():
        if k.startswith(pre):
            out.append((k[len(pre):], len(v)))
    out.sort(key=lambda x: -x[1])
    return out, total


def next_dest_share(ship, airline, x, ic):
    """航空会社 airline の機体が空港 x に着いた後、次に ic へ飛んだ割合（回数, 全体）"""
    nd = (ship or {}).get("nd", {}).get(f"{airline}|{x}") or {}
    total = sum(len(v) for v in nd.values())
    n = sum(len(v) for d, v in nd.items() if d == ic)
    return n, total


def process_live(net, cfg, state, watch, now_utc, tz, tt, pa=None):
    """広域走査（150nm）+ 監視機の全世界照会 + 希少機種の全世界照会
    → 在空機・到着予定/折り返し待ちの保持・発着記録・滑走路判定"""
    aps = cfg["airports"]
    R, W = cfg["scan_radius_nm"], cfg["wide_radius_nm"]
    nowts = now_utc.timestamp()
    seen = {}

    def add(a):
        if a["lat"] is None or not a["hex"]:
            return None
        return seen.setdefault(a["hex"], a)

    # 1) 広域走査: 近い空港どうしは1回の走査でまとめる（羽田と成田は約30nm）
    centers = []
    for ic in aps:
        lat, lon = airport_latlon(ic, state)
        if any(nm_between(lat, lon, c[0], c[1]) + R <= W for c in centers):
            continue
        centers.append((lat, lon))
        try:
            for x in net.area(lat, lon, W):
                add(ac_norm(x))
        except Exception as e:  # noqa: BLE001
            LOG.err(f"ADS-B 広域走査 {ic}", e)
    area_by_ap = {}
    for ic in aps:
        lat0, lon0 = airport_latlon(ic, state)
        area_by_ap[ic] = [a for a in seen.values() if nm_between(lat0, lon0, a["lat"], a["lon"]) <= R]

    # 2) 監視機（特別塗装機など）を登録記号で世界中から
    watched_air = []
    regs = sorted(watch)
    if regs:
        try:
            for a in (ac_norm(x) for x in net.regs(regs)):
                if a["reg"] not in watch:
                    continue
                o = add(a)
                if o:
                    watched_air.append(o)
        except Exception as e:  # noqa: BLE001
            LOG.err("ADS-B 監視機の照会", e)

    # 3a) 国内線には入らない機材・その空港には来ない機材を、日本周辺で探す（10分おき）。
    #     出発地を飛び立った時点で見つかるので、到着の1〜2時間前に分かる
    dom_types = domestic_candidate_types(cfg)
    if dom_types and nowts - state.get("gdom_at", 0) >= cfg.get("domestic_unusual_minutes", 10) * 60 - 60:
        try:
            n = 0
            for x in net.types(sorted(dom_types)):
                a = ac_norm(x)
                if a["lat"] is not None and in_japan(a["lat"], a["lon"]) and add(a):
                    n += 1
            state["gdom_at"], state["gdom_n"] = nowts, n
        except Exception as e:  # noqa: BLE001
            LOG.err("ADS-B 国内の珍しい機材の照会", e)

    # 3) 希少機種を型式で世界中から（30分おき）
    if nowts - state.get("gtype_at", 0) >= cfg["global_type_minutes"] * 60 - 60:
        try:
            n = 0
            for x in net.types(sorted(set(cfg["rare_types"]) - MIL_TYPES)):
                if add(ac_norm(x)):
                    n += 1
            state["gtype_at"] = nowts
            state["gtype_n"] = n
        except Exception as e:  # noqa: BLE001
            LOG.err("ADS-B 希少機種の全世界照会", e)
        # 国家元首機・政府機は機体番号（hex）で世界中から（相手国を出た時点で確定させるため）
        gcats = set((cfg.get("plane_alert") or {}).get("global_categories") or [])
        hexes = sorted(h for h, p in (pa or {}).items() if p[3] in gcats)
        if hexes:
            try:
                n = 0
                for x in net.hexes(hexes):
                    if add(ac_norm(x)):
                        n += 1
                state["gvip_n"] = n
            except Exception as e:  # noqa: BLE001
                LOG.err("ADS-B 政府機の全世界照会", e)
        state["gvip_total"] = len(hexes)

    # 4) 緊急信号（7700/7600/7500）を世界中から。撮影拠点から500nm以内のものだけ扱う
    for code in EMG_SQUAWK:
        try:
            for x in net.squawk(code):
                a = ac_norm(x)
                if a["lat"] is not None and min(nm_between(*airport_latlon(ic, state), a["lat"], a["lon"])
                                                for ic in aps) <= 500:
                    add(a)
        except Exception as e:  # noqa: BLE001
            LOG.err(f"ADS-B 緊急信号 {code} の照会", e)
            break

    # 分類（経路は不要）
    regulars = pa_regulars(cfg, state, dt.datetime.fromtimestamp(nowts, tz).date())
    ttx0 = tt_index(tt)
    today0 = dt.datetime.fromtimestamp(nowts, tz).date().isoformat()
    for a in seen.values():
        a["sev"], a["why"] = classify(a, watch, cfg, pa, regulars)
        a["fl"] = cs_to_fl(a["cs"], state)
        # 普段と違う機材（例: いつもB787の国内線にB777-300ER）。イレギュラー運航こそ撮りたい
        if a["sev"] in (None, "op") and a["fl"] and a["type"]:
            u = unusual_equipment(a, aps, ttx0, state.get("equip", {}), today0)
            if u:
                a["sev"], a["why"], a["unusual"] = "type", u, True
        lab = emg_label(a)
        if lab and not a["ground"]:
            a["emg"] = lab
            a["why"] = lab + (f"／{a['why']}" if a["why"] else "")
            a["sev"] = "emg"

    # 4) 経路（コールサイン→出発/到着）: 当日キャッシュ。注目機と空港近くの機体だけ問い合わせる
    rc = state.setdefault("routes", {})
    today = dt.datetime.fromtimestamp(nowts, tz).date().isoformat()
    for k in [k for k, v in rc.items() if v.get("d") != today]:
        del rc[k]
    near = {a["hex"] for lst in area_by_ap.values() for a in lst}
    ask = []
    for a in sorted(seen.values(), key=lambda a: (0 if a["sev"] in NOTABLE else 1)):
        if not a["cs"] or a["ground"] or a["cs"] in rc or not re.match(r"^[A-Z]{3}\d", a["cs"]):
            continue
        if a["sev"] in NOTABLE or a["hex"] in near or (a["type"] in dom_types and in_japan(a["lat"], a["lon"])):
            ask.append({"callsign": a["cs"], "lat": round(a["lat"], 3), "lng": round(a["lon"], 3)})
    if ask:
        try:
            for r in net.routeset(ask[:400]):
                cs = r.get("callsign")
                codes = [x.get("icao") for x in (r.get("_airports") or []) if x.get("icao")]
                rc[cs] = {"d": today, "ap": codes, "ok": bool(r.get("plausible", True)) and bool(codes)}
            state["routeset_fail"] = 0
        except Exception as e:  # noqa: BLE001
            state["routeset_fail"] = state.get("routeset_fail", 0) + 1
            LOG.err("経路照会（adsb.lol）", e)
            vrs_fallback(net, ask, rc, today, {a["cs"]: a for a in seen.values()})
    for a in seen.values():
        route = rc.get(a["cs"], {})
        a["route"] = route.get("ap") if route.get("ok") else None
        # 経路データに無い便でも、羽田・成田の公式時刻表に載っていれば行き先が分かる
        if not a["route"] and a.get("fl"):
            for ic in aps:
                for d_ in ("arr", "dep"):
                    row = ttx0.get((ic, today0, d_, a["fl"]))
                    if row:
                        code = row_code(row) or row.get("ap")
                        other = (iata_geo(code) or (code,))[0] if code else None
                        if other:
                            a["route"] = [other, ic] if d_ == "arr" else [ic, other]
                        break
                if a["route"]:
                    break
    live = list(seen.values())

    # 4b) 経路が分かった機体のうち、国内線に珍しい機材・その空港に珍しい機材
    for a in seen.values():
        if a["sev"] in (None, "op") and a["type"] in dom_types:
            u = domestic_unusual(a, cfg, aps)
            if u:
                a["sev"], a["why"], a["unusual"] = "type", u, True

    # 5) 到着予定（確定）と折り返し待ちを保持する。洋上では何時間も見えなくなるため、
    #    一度つかんだ到着予定は到着予想時刻まで消さない
    inb = state.setdefault("inbound", {})
    outb = state.setdefault("outbound", {})
    desc = []
    # 以前の版で機首の向きから推定した到着予定は使わない
    for k in [k for k, v in inb.items() if v.get("geo")]:
        del inb[k]
    # 以前の版では1機に古い便の折り返し待ちが何件も残っていた → 最新の1件だけにする
    latest = {}
    for k, v in outb.items():
        if v.get("hex") and (v["hex"] not in latest or v["eta_dest"] > outb[latest[v["hex"]]]["eta_dest"]):
            latest[v["hex"]] = k
    for k in [k for k, v in outb.items() if latest.get(v.get("hex")) != k
              or not returns_expected(v.get("reg"), v.get("sev"), v.get("orig"), v.get("dest"), aps)]:
        del outb[k]
    for a in seen.values():
        if a["ground"] or a["sev"] not in NOTABLE or not a["gs"]:
            continue
        key = f"{a['hex']}|{a['cs'] or '-'}"
        # 別の便名で飛んでいる＝前の便は終わった。前の便の到着予定・折り返し待ちは捨てる
        if a["cs"]:
            for d_ in (inb, outb):
                for k in [k for k, v in d_.items() if v.get("hex") == a["hex"] and v.get("cs") != a["cs"]]:
                    del d_[k]
        learn_leg(state, a, nowts, tz)
        base = {"hex": a["hex"], "reg": a["reg"], "type": a["type"], "cs": a["cs"], "fl": a["fl"],
                "sev": a["sev"], "why": a["why"], "updated": nowts}
        route = a.get("route")
        if route and len(route) >= 2:
            dest, orig = route[-1], route[0]
            g = iata_geo(dest) or ((dest,) + tuple(airport_latlon(dest, state)) if dest in aps else None)
            if not g:
                continue
            dist = nm_between(a["lat"], a["lon"], g[1], g[2])
            eta = nowts + dist / max(a["gs"], 150) * 3600 + 600
            if dest in aps:
                inb[key] = dict(base, dest=dest, orig=orig, eta=eta, dist=round(dist), geo=False,
                                first=inb.get(key, {}).get("first", nowts))
            elif returns_expected(a["reg"], a["sev"], orig, dest, aps):
                # 戻ってくると見込める区間だけ記録し、そこから戻る便を予測する
                outb[key] = dict(base, orig=orig, dest=dest, eta_dest=eta)
        elif a["alt"] is not None and a["alt"] < 25000 and (a["vr"] or 0) < -300:
            # 経路不明: 空港付近では進入経路に沿って大きく曲がるため、機首の向きでは行き先を決めない。
            # 「撮影拠点の周辺で降下中（行き先不明）」として画面に出すだけ（通知しない）
            ds = {ic: nm_between(*airport_latlon(ic, state), a["lat"], a["lon"]) for ic in aps}
            if min(ds.values()) <= W:
                desc.append({"hex": a["hex"], "reg": a["reg"], "type": a["type"], "cs": a["cs"], "sev": a["sev"],
                             "why": a["why"], "alt": int(a["alt"]), "dist": {k: round(v) for k, v in ds.items()},
                             "lat": round(a["lat"], 3), "lon": round(a["lon"], 3)})
    state["descending"] = sorted(desc, key=lambda x: min(x["dist"].values()))[:12]
    # 緊急信号の機体（画面の先頭に出す）
    emg = []
    for a in seen.values():
        if a.get("emg"):
            route = a.get("route") or []
            emg.append({"hex": a["hex"], "reg": a["reg"], "type": a["type"], "cs": a["cs"], "fl": a.get("fl"),
                        "label": a["emg"], "why": a["why"], "alt": None if a["alt"] is None else int(a["alt"]),
                        "route": route, "to": route[-1] if route else None,
                        "routeTxt": "→".join(ap_label(c) for c in route) if len(route) >= 2 else "",
                        "dist": {ic: round(nm_between(*airport_latlon(ic, state), a["lat"], a["lon"])) for ic in aps},
                        "lat": round(a["lat"], 3), "lon": round(a["lon"], 3)})
    state["emergencies"] = sorted(emg, key=lambda x: min(x["dist"].values()))
    # 注目機の航跡（誤り報告の調査用。最大6時間・24点）
    tracks = state.setdefault("tracks", {})
    for a in seen.values():
        if a.get("sev") and a["lat"] is not None:
            lst = tracks.setdefault(a["hex"], [])
            lst.append([int(nowts), round(a["lat"], 4), round(a["lon"], 4), None if a["alt"] is None else int(a["alt"]),
                        None if a["trk"] is None else int(a["trk"]), None if a["gs"] is None else int(a["gs"]),
                        None if a["vr"] is None else int(a["vr"]), a["cs"], a.get("sev"), a["reg"], a.get("fl")])
            del lst[:-24]
    for h in list(tracks):
        tracks[h] = [x for x in tracks[h] if nowts - x[0] < 6 * 3600]
        if not tracks[h]:
            del tracks[h]
    for k in [k for k, v in inb.items() if nowts > v["eta"] + 1800]:
        del inb[k]
    # 折り返し待ちの有効期限: 国内の空港に着いた機体は20時間（夜間駐機して翌朝飛ぶところまで）、
    # 海外は36時間。その間にどこかを飛んでいるのを見れば、上の処理でその時点で消える
    for k in [k for k, v in outb.items()
              if nowts > v["eta_dest"] + (20 if (v.get("dest") or "")[:2] in ("RJ", "RO") else 36) * 3600]:
        del outb[k]

    # 6) 各空港: 滑走路判定と発着記録（45nm圏）
    ttx = tt_index(tt)
    rw_state = state.setdefault("runway_live", {})
    mv = state.setdefault("mv", {})
    for ic in aps:
        acs = area_by_ap.get(ic, [])
        arr, dep = detect_runway(ic, acs)
        code = config_from_votes(ic, arr, dep)
        if code:
            rw_state[ic] = {"code": code, "arr": arr, "dep": dep, "at": nowts}
        elif rw_state.get(ic) and nowts - rw_state[ic]["at"] > 2400:
            rw_state[ic]["stale"] = True
        record_movements(ic, acs, arr, dep, a_lookup=None, mv=mv, state=state, now_utc=now_utc, tz=tz, ttx=ttx, aps=aps)
    # 最終進入に入って空港が分かった機体・到着予定がある機体は「行き先不明で降下中」から外す
    known = {v["hex"] for v in mv.values() if nowts - v.get("last", 0) < 60} | {v["hex"] for v in inb.values()}
    state["descending"] = [x for x in state.get("descending", []) if x["hex"] not in known]
    return live, watched_air, area_by_ap


def _route_plausible(codes, lat, lon):
    """位置が出発地→到着地の経路から大きく外れていないか（座標の分かる空港だけで判定）"""
    pts = []
    for c in codes:
        g = iata_geo(c)
        if not g:
            return True
        pts.append((g[1], g[2]))
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        leg = nm_between(la1, lo1, la2, lo2)
        if nm_between(la1, lo1, lat, lon) + nm_between(lat, lon, la2, lo2) <= leg * 1.25 + 150:
            return True
    return False


def vrs_fallback(net, ask, rc, today, by_cs, max_airlines=15):
    """adsb.lol の経路照会が使えないとき、同じ元データ（VRS standing-data, GitHub）を航空会社ごとに読む"""
    import csv
    import io
    want = {}
    for p in ask:
        want.setdefault(p["callsign"][:3], []).append(p["callsign"])
    n = 0
    for airline, css in want.items():
        if n >= max_airlines:
            break
        n += 1
        try:
            table = {}
            for r in csv.DictReader(io.StringIO(net.vrs_routes(airline).lstrip("\ufeff"))):
                table[(r.get("Callsign") or "").strip()] = (r.get("AirportCodes") or "").strip()
        except Exception as e:  # noqa: BLE001
            LOG.err(f"経路データ（予備） {airline}", e)
            continue
        for cs in css:
            codes = [c for c in table.get(cs, "").split("-") if c]
            a = by_cs.get(cs)
            ok = bool(codes) and (a is None or _route_plausible(codes, a["lat"], a["lon"]))
            rc[cs] = {"d": today, "ap": codes, "ok": ok, "src": "vrs"}


def movement_direction(ic, a, ttx, date_iso):
    """この空港での到着か出発か。(dir, fl, 相手地, 根拠)"""
    fl = a.get("fl")
    if fl:
        for d in ("arr", "dep"):
            r = ttx.get((ic, date_iso, d, fl))
            if r:
                return d, fl, row_code(r) or r["ap"], "時刻表"
    route = a.get("route")
    if route and len(route) >= 2:
        if route[-1] == ic:
            return "arr", fl, route[-2], "経路"
        if route[0] == ic:
            return "dep", fl, route[1], "経路"
        if ic in route[1:-1]:
            return None, fl, None, "経由"
        return None, fl, None, "他空港"
    return None, fl, None, "不明"


def runway_gate(ic, a):
    """滑走路の延長線上で最終進入中（arr）／離陸直後（dep）なら (向き, 滑走路)"""
    if a["ground"] or a["trk"] is None or a["alt"] is None:
        return None
    for e in airport_ends(ic):
        x, y = local_xy(e["lat"], e["lon"], a["lat"], a["lon"])
        h = math.radians(e["hdg"])
        along = x * math.sin(h) + y * math.cos(h)
        cross = x * math.cos(h) - y * math.sin(h)
        if ang_diff(a["trk"], e["hdg"]) > 22:
            continue
        # 最終進入: 滑走路の延長線上20nm以内（5〜10分おきの観測でも取り逃がさないよう、合流直後から拾う）
        if -20 <= along <= 0.3 and a["alt"] < 1500 + 350 * abs(along) and a["alt"] < 7500 \
                and abs(cross) < 0.6 + 0.05 * abs(along) and (a["vr"] is None or a["vr"] <= 300):
            return ("arr", e["id"])
        if e["len"] <= along <= e["len"] + 7 and abs(cross) < 1.0 and a["alt"] < 1800 + 700 * (along - e["len"]) \
                and (a["vr"] is None or a["vr"] >= 0):
            return ("dep", e["id"])
    return None


def record_movements(ic, acs, arr_votes, dep_votes, a_lookup, mv, state, now_utc, tz, ttx, aps=None):
    lat0, lon0 = airport_latlon(ic, state)
    ends = airport_ends(ic)
    nowts = now_utc.timestamp()
    for a in acs:
        if not a["hex"] or a["alt"] is None:
            continue
        d = nm_between(lat0, lon0, a["lat"], a["lon"])
        local_date = dt.datetime.fromtimestamp(nowts, tz).date().isoformat()
        direction, fl, other, basis = movement_direction(ic, a, ttx, local_date)
        gnd_dep = False
        if a["ground"]:
            if d > 3:
                continue
            # 地上で信号を出し始めた機体（プッシュバック・エンジン始動後など）。
            # 出発便名なら「出発準備中」、着陸して間もない機体は到着、便名なしで止まっていれば駐機
            recent_arr = any(v.get("hex") == a["hex"] and v.get("dir") == "arr" and v.get("ap") == ic
                             and nowts - v.get("last", 0) < 5400 for v in mv.values())
            if direction == "dep":
                gnd_dep = True
            elif direction == "arr" or recent_arr:
                direction = "arr"
            elif a["cs"] and (a["gs"] or 0) >= 3:
                direction, gnd_dep, basis = "dep", True, "地上走行"
            else:
                direction = "ground"
        elif direction is None:
            # 経路不明の機体は、機首の向きでは判定しない（空港付近は進入経路に沿って大きく曲がるため）。
            # 滑走路の延長線上の最終進入・離陸直後、または空港のごく近く（5nm以内・2,000ft未満）だけで判定する
            gate = runway_gate(ic, a)
            if gate:
                direction = gate[0]
            elif a["alt"] < 2000 and d < 5 and a["vr"] is not None and abs(a["vr"]) >= 300:
                direction = "arr" if a["vr"] < 0 else "dep"
            else:
                continue
            if a["sev"] == "mil" and direction == "arr":
                for code, (nm_, la, lo) in MIL_FIELDS.items():
                    if nm_between(la, lo, a["lat"], a["lon"]) < d * 0.7:
                        direction = None
                        break
            if not direction:
                continue
            basis = "位置"
        if direction == "ground":
            key = f"{ic}|ground|{a['hex']}|{local_date}"
        else:
            key = f"{ic}|{direction}|{a['hex']}|{a['cs'] or '-'}|{local_date}"
        gs = max(a["gs"] or 0, 120)
        if direction == "arr":
            evt = nowts + (d / gs) * 3600 + (0 if a["ground"] else 120)
        elif gnd_dep:
            # 離陸時刻の見込み: 時刻表の出発時刻（変更後）か、地上で見えてから10分後の遅い方
            row = ttx.get((ic, local_date, "dep", fl)) if fl else None
            sched = None
            if row:
                m_ = _hhmm_to_min(row.get("rev") or row.get("time"))
                if m_ is not None:
                    sched = dt.datetime.combine(dt.date.fromisoformat(local_date), dt.time(m_ // 60, m_ % 60), tz).timestamp()
            evt = max(sched or 0, nowts + 600) if sched else nowts + 900
        elif direction == "dep":
            evt = nowts - (d / gs) * 3600
        else:
            evt = nowts
        rec = mv.get(key)
        rwy = None
        if not a["ground"]:
            for e in ends:
                x, y = local_xy(e["lat"], e["lon"], a["lat"], a["lon"])
                h = math.radians(e["hdg"])
                along = x * math.sin(h) + y * math.cos(h)
                cross = x * math.cos(h) - y * math.sin(h)
                if a["trk"] is not None and ang_diff(a["trk"], e["hdg"]) <= 22:
                    if direction == "arr" and -12 <= along <= 0.3 and a["alt"] < 4500 and abs(cross) < 0.6 + 0.04 * abs(along):
                        rwy = e["id"]
                    if direction == "dep" and e["len"] <= along <= e["len"] + 7 and abs(cross) < 1.0 and a["alt"] < 1800 + 700 * (along - e["len"]):
                        rwy = e["id"]
        if not rec:
            rec = {"ap": ic, "dir": direction, "hex": a["hex"], "reg": a["reg"], "type": a["type"],
                   "cs": a["cs"], "fl": fl, "other": other, "basis": basis, "sev": a["sev"], "why": a["why"],
                   "first": nowts, "last": nowts, "evt": evt, "rwy": rwy, "date": local_date,
                   "landed": a["ground"] and direction == "arr", "gnd": gnd_dep, "gnd_at": nowts if gnd_dep else None}
            mv[key] = rec
        else:
            rec["last"] = nowts
            # 着陸後は、最後の空中位置から見積もった接地時刻を保持する
            if direction == "arr" and not rec.get("landed") and not a["ground"]:
                rec["evt"] = evt
            if direction == "dep":
                if gnd_dep:
                    if rec.get("gnd"):
                        rec["evt"] = evt  # まだ地上: 遅れに合わせて更新
                elif rec.get("gnd"):
                    rec["evt"], rec["gnd"], rec["airborne"] = evt, False, True  # 離陸を確認
                else:
                    rec["evt"] = min(rec["evt"], evt)
            if rwy:
                rec["rwy"] = rwy
            if a["ground"] and direction == "arr":
                rec["landed"] = True
            rec["reg"] = rec["reg"] or a["reg"]
            rec["type"] = rec["type"] or a["type"]
            rec["fl"] = rec["fl"] or fl
            rec["other"] = rec["other"] or other
        rec["evt_date"] = dt.datetime.fromtimestamp(rec["evt"], tz).date().isoformat()
    # 古い記録の整理（48時間）
    for k in [k for k, v in mv.items() if nowts - v["last"] > 48 * 3600]:
        del mv[k]


# --------------------------------------------------------------------------
# 学習（便ごとの機材・監視機の来訪パターン・レア機の定期性）
# --------------------------------------------------------------------------
def learn(state, cfg, watch, now_utc, tz, pa=None):
    keep = cfg["history_days"] + 7
    cutoff = (dt.datetime.fromtimestamp(now_utc.timestamp(), tz).date() - dt.timedelta(days=keep)).isoformat()
    equip = state.setdefault("equip", {})
    regdays = state.setdefault("regdays", {})
    rare = state.setdefault("rare", {})
    state.pop("learned", None)
    mv = state.get("mv", {})
    for key, m in list(mv.items()):
        if m["dir"] not in ("arr", "dep"):
            continue
        # 到着は着陸後、出発は圏外に出た後に確定させる
        final = (m["dir"] == "arr" and m.get("landed")) or (now_utc.timestamp() - m["last"] > 1500)
        if not final or m.get("learned"):
            continue
        m["learned"] = True
        hhmm = dt.datetime.fromtimestamp(m["evt"], tz).strftime("%H:%M")
        date = m["evt_date"]
        if (m.get("fl") or m.get("cs")) and m.get("type"):
            lst = equip.setdefault(f"{m['ap']}|{m['dir']}|{m.get('fl') or m['cs']}", [])
            if not any(x[0] == date for x in lst):
                lst.append([date, m["type"], m.get("reg", ""), hhmm])
                del lst[:-6]
        if m.get("reg") and m["reg"] in watch:
            day = regdays.setdefault(m["reg"], {}).setdefault(date, [])
            day.append({"ap": m["ap"], "dir": m["dir"], "t": hhmm, "fl": m.get("fl") or m.get("cs"),
                        "other": m.get("other"), "rwy": m.get("rwy")})
        if pa and m["hex"] in pa:
            ds = state.setdefault("pa_seen", {}).setdefault(m["hex"], [])
            if date not in ds:
                ds.append(date)
                del ds[:-20]
        if m.get("sev") in ("type", "mil", "op"):
            k2 = f"{m['ap']}|{m['dir']}|{m.get('fl') or m.get('cs') or m.get('reg') or m['hex']}"
            lst = rare.setdefault(k2, [])
            if not any(x["date"] == date for x in lst):
                lst.append({"date": date, "t": hhmm, "reg": m.get("reg"), "type": m.get("type"),
                            "sev": m["sev"], "why": m.get("why"), "cs": m.get("cs"), "other": m.get("other"),
                            "rwy": m.get("rwy")})
    # 学習済みで注目機でもない記録は捨てる（状態ファイルを小さく保つ）
    for k in [k for k, m in mv.items() if m.get("learned") and not m.get("sev")]:
        del mv[k]
    for k in list(equip):
        equip[k] = [x for x in equip[k] if x[0] >= cutoff]
        if not equip[k]:
            del equip[k]
    for r in list(regdays):
        for d in list(regdays[r]):
            if d < cutoff:
                del regdays[r][d]
    for k in list(rare):
        rare[k] = [x for x in rare[k] if x["date"] >= cutoff]
        if not rare[k]:
            del rare[k]
    prune_ship(state, dt.datetime.fromtimestamp(now_utc.timestamp(), tz).date())
    pas = state.get("pa_seen", {})
    for h in list(pas):
        pas[h] = [d for d in pas[h] if d >= cutoff]
        if not pas[h]:
            del pas[h]
    # 監視機の「飛んでいた日」（整備などで休んでいた日を分母から外すため）
    active = state.setdefault("reg_active", {})
    today = dt.datetime.fromtimestamp(now_utc.timestamp(), tz).date().isoformat()
    for a in state.get("_watched_air_regs", []):
        lst = active.setdefault(a, [])
        if today not in lst:
            lst.append(today)
            del lst[:-30]


# --------------------------------------------------------------------------
# 計画づくり
# --------------------------------------------------------------------------
CONF_W = {"確定": 1.0, "有力": 0.75, "傾向": 0.35}


def build_plan(cfg, state, watch, live, watched_air, tt, wx, now_utc, tz, pa=None):
    now_local = dt.datetime.fromtimestamp(now_utc.timestamp(), tz)
    days = [now_local.date(), now_local.date() + dt.timedelta(days=1)]
    ttx = tt_index(tt)
    plan = {"generated": now_utc.isoformat(), "version": VERSION, "tz": cfg["timezone"], "airports": {}}
    for ic in cfg["airports"]:
        ap_out = {"icao": ic, "name": AIRPORT_INFO.get(ic, {}).get("name") or ap_label(ic),
                  "iata": AIRPORT_INFO.get(ic, {}).get("iata", ""), "days": {},
                  "timetable": bool(AIRPORT_INFO.get(ic, {}).get("timetable")),
                  "configs": {c: {"label": CONFIG_LABEL.get(ic, {}).get(c, c),
                                  "short": CONFIG_SHORT.get(ic, {}).get(c, c),
                                  "arr": CONFIG_RUNWAYS.get(ic, {}).get(c, ("", ""))[0],
                                  "dep": CONFIG_RUNWAYS.get(ic, {}).get(c, ("", ""))[1]}
                              for c in CONFIG_LABEL.get(ic, {"X": ""})},
                  "lat": airport_latlon(ic, state)[0], "lon": airport_latlon(ic, state)[1]}
        live_cfg = state.get("runway_live", {}).get(ic)
        if live_cfg and live_cfg.get("stale"):
            live_cfg = None
        ap_out["now"] = {
            "runway": None if not live_cfg else {
                "code": live_cfg["code"], "label": CONFIG_LABEL.get(ic, {}).get(live_cfg["code"], live_cfg["code"]),
                "arr": live_cfg.get("arr"), "dep": live_cfg.get("dep"),
                "at": dt.datetime.fromtimestamp(live_cfg["at"], tz).strftime("%H:%M")},
            "metar": (wx.get(ic) or {}).get("metar"),
            "taf": ((wx.get(ic) or {}).get("taf") or {}).get("raw"),
        }
        carry = (live_cfg or {}).get("code")
        for day in days:
            hours, sun = hourly_table(ic, wx, day, tz, live_cfg, now_utc,
                                      start_code=carry if day == days[0] else hours[-1]["cfg"])
            events = events_for(ic, day, cfg, state, watch, live, watched_air, tt, ttx, now_utc, tz)
            for ev in events:
                h = int(ev["time"][:2]) if ev.get("time") else (ev["hours"][0] if ev.get("hours") else 12)
                row = hours[min(max(h, 0), 23)]
                ev["rwyCfg"] = row["cfg"]
                ev["rwy"] = ev.get("rwyActual") or predict_runway(ic, row["cfg"], ev["dir"], ev.get("otherCode") or ev.get("other"), ev.get("type"), cfg)
                ev["rwyConf"] = "実績" if ev.get("rwyActual") else row["cfgConf"]
                ev["shoot"] = row["shoot"]
                ev["shootWhy"] = row.get("shootWhy")
            events.sort(key=lambda e: (e.get("time") or "%02d:00" % (e["hours"][0] if e.get("hours") else 23)))
            ap_out["days"][day.isoformat()] = {
                "label": "今日" if day == days[0] else "明日",
                "events": events, "hours": hours, "sun": sun,
                "summary": summarize(ic, day, events, hours, now_local),
            }
        plan["airports"][ic] = ap_out
    plan["registry"] = {
        "active": sorted([{"reg": r, **v} for r, v in watch.items()], key=lambda x: x["reg"]),
        "changelog": list(reversed(state.get("changelog", [])[-30:])),
        "meta": state.get("registry_meta", {}),
    }
    pm = state.get("pa_meta", {})
    regs_ = pa_regulars(cfg, state, now_local.date())
    plan["planeAlert"] = {
        "off": bool(pm.get("off")), "count": pm.get("count", 0), "date": pm.get("date"),
        "groups": [PA_LABEL.get(g, g) for g in ((cfg.get("plane_alert") or {}).get("groups") or [])],
        "global": state.get("gvip_total", 0),
        "regulars": sorted([{"hex": h, "reg": (pa or {}).get(h, ["?"])[0], "name": PA_LABEL.get((pa or {}).get(h, [""] * 5)[4], "")
                             + "（" + ((pa or {}).get(h, ["", "?"])[1]) + "）",
                             "days": len(state.get("pa_seen", {}).get(h, []))} for h in regs_ if h in (pa or {})],
                           key=lambda x: -x["days"])[:30],
    }
    plan["learning"] = {"days": len({d for r in state.get("regdays", {}).values() for d in r} |
                                    {x[0] for v in state.get("equip", {}).values() for x in v}),
                        "flights": len(state.get("equip", {}))}
    return plan


def _hhmm_to_min(s):
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return None


def events_for(ic, day, cfg, state, watch, live, watched_air, tt, ttx, now_utc, tz):
    d_iso = day.isoformat()
    today = dt.datetime.fromtimestamp(now_utc.timestamp(), tz).date()
    is_today = day == today
    rows_tt = (tt.get(ic) or {}).get(d_iso, [])
    events = {}

    def put(ev):
        key = ev["key"]
        cur = events.get(key)
        if not cur or CONF_W.get(ev["conf"], 0) > CONF_W.get(cur["conf"], 0):
            events[key] = ev

    lat0, lon0 = airport_latlon(ic, state)

    # (A) 今日: すでに起きた/いま起きている監視・レア機の発着（実測）
    if is_today:
        for k, m in state.get("mv", {}).items():
            if m["ap"] != ic or m.get("evt_date") != d_iso or not m.get("sev"):
                continue
            if m["sev"] == "op" and m["dir"] == "ground":
                continue
            t = dt.datetime.fromtimestamp(m["evt"], tz)
            tt_row = ttx.get((ic, d_iso, m["dir"], m.get("fl"))) if m.get("fl") else None
            nts = now_utc.timestamp()
            past = m["evt"] < nts - 300
            reason = "最終進入・離陸直後の位置で確認（経路情報なし）" if m.get("basis") == "位置" else "ADS-Bで確認"
            if m["dir"] == "dep" and m.get("gnd"):
                # 地上で信号を確認した出発便（プッシュバック・エンジン始動後など）。見えなくなって20分で済とする
                past = nts - m["last"] > 1200
                status = "出発準備中"
                reason = (dt.datetime.fromtimestamp(m.get("gnd_at") or m["first"], tz).strftime("%H:%M")
                          + " に地上で信号を確認（プッシュバック・地上走行中）。離陸はおよそ10〜25分後")
            elif m["dir"] == "ground":
                past = nts - m["last"] > 1200
                status = "駐機中"
                reason = dt.datetime.fromtimestamp(m["last"], tz).strftime("%H:%M") + " 時点で地上に駐機（便名なし）"
            else:
                status = ("到着済" if m.get("landed") else ("着陸見込み" if not past else "到着済")) if m["dir"] == "arr" \
                    else "出発済"
            put({"key": f"{m['dir']}|{m.get('fl') or m['hex']}", "dir": m["dir"], "time": t.strftime("%H:%M"),
                 "fl": m.get("fl") or m.get("cs"), "reg": m.get("reg"), "hex": m["hex"], "type": m.get("type"),
                 "name": m.get("why"), "cat": m["sev"],
                 "conf": "確定", "status": status,
                 "other": ap_label(m.get("other")) if m.get("other") and not tt_row else (tt_row or {}).get("ap", ""),
                 "otherCode": m.get("other"), "sched": (tt_row or {}).get("time"), "rwyActual": m.get("rwy"),
                 "reason": reason, "past": past})

    # (B) 飛行中の注目機: この空港へ向かっている（確定）／この空港を出て折り返してくる（有力）
    nowts = now_utc.timestamp()
    for k, r in state.get("inbound", {}).items():
        if r["dest"] != ic:
            continue
        t = dt.datetime.fromtimestamp(r["eta"], tz)
        if t.date() != day:
            continue
        tt_row = ttx.get((ic, d_iso, "arr", r.get("fl"))) if r.get("fl") else None
        left = max(0, (r["eta"] - nowts) / 3600)
        left_txt = f"約{left:.0f}時間" if left >= 1.5 else f"約{max(1, round(left * 60))}分"
        if r.get("geo"):
            reason = f"{r['dist']}nm圏内で降下しながら接近中（到着まで{left_txt}・経路情報なし）"
        else:
            reason = f"{ap_label(r['orig'])}を出発済み・飛行中（到着まで{left_txt}）"
        # 到着まで2時間以上ある間は、概算より航空会社の定刻（変更時刻）の方が当てになる
        if tt_row and left >= 2:
            shown = tt_row.get("rev") or tt_row.get("time")
            reason += f"。実測からの概算は{t.strftime('%H:%M')}頃"
        else:
            shown = t.strftime("%H:%M")
        put({"key": f"arr|{r.get('fl') or r['hex']}", "dir": "arr",
             "time": shown, "sched": (tt_row or {}).get("time"),
             "eta": t.strftime("%H:%M"), "fl": r.get("fl") or r.get("cs"), "reg": r.get("reg"), "hex": r["hex"],
             "type": r.get("type"), "name": watch.get(r.get("reg"), {}).get("name") or r.get("why"),
             "cat": r["sev"], "conf": "有力" if r.get("geo") else "確定", "status": "飛行中",
             "other": (tt_row or {}).get("ap") or ap_label(r.get("orig")), "otherCode": r.get("orig"),
             "reason": reason, "past": False, "leftH": round(left, 1)})
    for k, r in state.get("outbound", {}).items():
        if r["dest"] == ic:
            continue
        ret = predict_return(ic, r, tt, tz, day, state, nowts, cfg["airports"])
        if ret:
            put(ret | {"reg": r.get("reg"), "hex": r["hex"], "type": r.get("type"),
                       "name": watch.get(r.get("reg"), {}).get("name") or r.get("why"), "cat": r["sev"]})

    # (C) 時刻表 × 学習した機材（羽田）
    equip = state.get("equip", {})
    rare_types = set(cfg["rare_types"])
    for r in rows_tt:
        hist = equip.get(f"{ic}|{r['dir']}|{r['fl']}") or []
        if len(hist) < 2:
            continue
        types = [x[1] for x in hist]
        regs = [x[2] for x in hist if x[2]]
        rare_n = sum(1 for t in types if t in rare_types or t in MIL_TYPES)
        watch_n = sum(1 for x in regs if x in watch)
        if rare_n / len(types) >= 0.5:
            top = max(set(types), key=types.count)
            put({"key": f"{r['dir']}|{r['fl']}", "dir": r["dir"], "time": r["rev"] or r["time"], "sched": r["time"],
                 "fl": r["fl"], "reg": None, "type": top, "name": f"{top} で運航されることが多い便",
                 "cat": "type", "conf": "有力" if rare_n >= 3 else "傾向", "status": r.get("status", ""),
                 "other": r["ap"], "otherCode": row_code(r),
                 "reason": f"直近{len(types)}回中{rare_n}回が{top}", "past": r.get("cat") in ("arrived", "departed")})
        elif watch_n >= 2 and watch_n / len(regs) >= 0.4:
            top = max(set(x for x in regs if x in watch), key=regs.count)
            put({"key": f"{r['dir']}|{r['fl']}", "dir": r["dir"], "time": r["rev"] or r["time"], "sched": r["time"],
                 "fl": r["fl"], "reg": top, "type": None, "name": watch[top]["name"], "cat": "watch", "conf": "傾向",
                 "status": r.get("status", ""), "other": r["ap"], "otherCode": row_code(r),
                 "reason": f"直近{len(regs)}回中{watch_n}回を{top}が担当", "past": r.get("cat") in ("arrived", "departed")})

    # (D) 監視機の来訪パターン（明日、または今日まだ動いていない機体）
    regdays = state.get("regdays", {})
    active_days = state.get("reg_active", {})
    today_seen = {a["reg"] for a in watched_air} | {e["reg"] for e in events.values() if e.get("reg")}
    covered = {x for e in events.values() for x in (e.get("fl"), e.get("reg"), e.get("hex")) if x}
    H = cfg["history_days"]
    for reg, days in regdays.items():
        if reg not in watch or (is_today and reg in today_seen):
            continue
        window = [(day - dt.timedelta(days=i)).isoformat() for i in range(1, H + 1)]
        visits = [d for d in window if any(x["ap"] == ic for x in days.get(d, []))]
        flown = set(active_days.get(reg, [])) | {d for d in days}
        flown = [d for d in window if d in flown]
        if not visits or len(visits) < 2:
            continue
        hours, detail = {}, {}
        for d in visits:
            for x in days[d]:
                if x["ap"] == ic and hhmm_ok(x.get("t")):
                    hh = int(x["t"][:2])
                    hours[hh] = hours.get(hh, 0) + 1
                    detail.setdefault(hh, set()).add(x["dir"])
        now_h = dt.datetime.fromtimestamp(now_utc.timestamp(), tz).hour
        if is_today:
            hours = {h: n for h, n in hours.items() if h > now_h}
            if not hours:
                continue
        top_hours = sorted(sorted(hours, key=lambda h: -hours[h])[:4])
        ratio = len(visits) / max(1, len(flown))
        hours_detail = [{"h": h, "n": hours[h], "dir": "/".join(sorted(detail.get(h, [])))} for h in top_hours]
        put({"key": f"pattern|{reg}", "dir": "both", "time": None, "hours": top_hours, "fl": None, "reg": reg,
             "type": None, "name": watch[reg]["name"], "cat": "watch", "conf": "傾向",
             "reason": f"直近{H}日で{AIRPORT_INFO.get(ic, {}).get('name', ic)}に来た日 {len(visits)}日"
                       f"（飛行を確認した{len(flown)}日中）",
             "ratio": round(ratio, 2), "visits": len(visits), "hoursDetail": hours_detail,
             "status": "", "other": "", "past": False})

    # (E) レア機の定期性（成田の貨物・軍用機など、時刻表に無いもの）
    dow = day.weekday()
    for k, lst in state.get("rare", {}).items():
        ap, direction, ident = k.split("|", 2)
        if ap != ic or (is_today and ident in covered):
            continue
        past = [x for x in lst if x["date"] < d_iso]
        dates = sorted({x["date"] for x in past})
        if len(dates) < 2:
            continue
        dows = {dt.date.fromisoformat(x).weekday() for x in dates}
        span = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days
        n7 = len([x for x in dates if x >= (day - dt.timedelta(days=7)).isoformat()])
        same_dow = [x for x in past if dt.date.fromisoformat(x["date"]).weekday() == dow]
        if n7 >= 5 or len(dows) >= 6:
            conf, basis = "有力", past
            reason = f"ほぼ毎日（直近7日中{n7}日）"
        elif span >= 9 and len(dates) >= 4:
            # 2週以上観測できていて、決まった曜日にだけ来る便
            if dow not in dows:
                continue
            conf, basis = "有力", same_dow
            reason = "・".join("月火水木金土日"[w] for w in sorted(dows)) + "曜に来ている便"
        elif same_dow:
            conf, basis = "傾向", same_dow
            reason = f"先週の同じ曜日に来た（直近{H}日で{len(dates)}回）"
        else:
            if len(dates) < 3:
                continue
            conf, basis = "傾向", past
            reason = f"直近{H}日で{len(dates)}回"
        mins = sorted(m for m in (_hhmm_to_min(x["t"]) for x in basis) if m is not None)
        med = mins[len(mins) // 2] if mins else None
        last = basis[-1]
        cat = "mil" if last["sev"] == "mil" else ("type" if last["sev"] == "type" else "op")
        put({"key": f"{direction}|{ident}", "dir": direction,
             "time": None if med is None else "%02d:%02d" % (med // 60, med % 60), "approx": True,
             "fl": last.get("cs") or ident, "reg": last.get("reg"), "type": last.get("type"),
             "name": last.get("why") if last.get("why") and cat != "op" else
                     {"mil": "軍用・政府機", "type": f"希少機種 {last.get('type')}", "op": "珍しい運航者"}[cat],
             "cat": cat, "conf": conf, "status": "", "other": ap_label(last.get("other")),
             "otherCode": last.get("other"), "reason": reason, "past": False})

    out = list(events.values())
    for e in out:
        e["time"] = hhmm_ok(e.get("time")) or None
        if e.get("sched"):
            e["sched"] = hhmm_ok(e["sched"]) or None
        if not e.get("hours"):
            e["hours"] = [int(e["time"][:2])] if e.get("time") else []
        e["ap"] = ic
        # 今日の便で、時刻を20分以上過ぎたもの（飛行中を除く）は「済」扱い
        if is_today and e.get("time") and e.get("status") not in ("飛行中", "着陸見込み"):
            m = _hhmm_to_min(e["time"])
            now_m = dt.datetime.fromtimestamp(now_utc.timestamp(), tz)
            if m is not None and m < now_m.hour * 60 + now_m.minute - 20:
                e["past"] = True
    # 珍しい運航者は「確定」以外は出しすぎないように上限
    ops = [e for e in out if e["cat"] == "op" and e["conf"] != "確定"]
    if len(ops) > 12:
        drop = {id(e) for e in sorted(ops, key=lambda e: CONF_W[e["conf"]])[:-12]}
        out = [e for e in out if id(e) not in drop]
    return out


def _tt_arrival(tt, ic, fl, after, tz):
    """時刻表で ic に着く便 fl の、after 以降で最初の到着 (時刻, 行)"""
    best = None
    for d_iso2, rows in (tt.get(ic) or {}).items():
        if d_iso2.startswith("_"):
            continue
        d2 = dt.date.fromisoformat(d_iso2)
        for row in rows:
            if row["dir"] != "arr" or row["fl"] != fl:
                continue
            m = _hhmm_to_min(row.get("rev") or row["time"])
            if m is None:
                continue
            tm = dt.datetime.combine(d2, dt.time(m // 60, m % 60), tz)
            if tm >= after - dt.timedelta(minutes=10) and (best is None or tm < best[0]):
                best = (tm, row)
    return best


def _first_returns(ap, r, tt, tz, arr_dest, prefix, dest_iata, names):
    """dest から ap へ戻る便の候補（時刻順）。折り返し時間と飛行時間を見込んだ最早時刻以降"""
    g_d, g_h = iata_geo(r["dest"]), AIRPORT_INFO.get(ap)
    if not g_d or not g_h or not prefix:
        return [], False, None
    dist = nm_between(g_d[1], g_d[2], g_h["lat"], g_h["lon"])
    long_haul = dist > 2500
    block = dist / (470 if long_haul else 430) * 60 + (30 if long_haul else 25)
    earliest = arr_dest + dt.timedelta(minutes=(150 if long_haul else 40) + block)
    out = []
    for d_iso2, rows in (tt.get(ap) or {}).items():
        if d_iso2.startswith("_"):
            continue
        d2 = dt.date.fromisoformat(d_iso2)
        for row in rows:
            if row["dir"] != "arr" or (row["ap"] not in names and row.get("apc") != dest_iata) \
                    or not row["fl"].startswith(prefix):
                continue
            m = _hhmm_to_min(row["time"])
            if m is None:
                continue
            tm = dt.datetime.combine(d2, dt.time(m // 60, m % 60), tz)
            if earliest - dt.timedelta(minutes=10) <= tm <= earliest + dt.timedelta(hours=30):
                out.append((tm, row))
    out.sort(key=lambda x: x[0])
    return out, long_haul, earliest


def predict_return(ic, r, tt, tz, day, state=None, nowts=None, aps=None):
    """撮影拠点以外へ向かっている注目機が、ic に戻ってくる便を予測する。
    1) 機材繰りの学習（同じ機体が続けて乗った便の記録）をたどり、羽田・成田どちらかの着便に行き着けばそれを採用
    2) 行き先から戻る最初の便（時刻表）。羽田・成田の両方に候補があれば（例: ANAの777がHND→SFO→NRT）
       どちらとも言えないので「傾向」とし、もう一方の候補も理由欄に書く
    3) 時刻表の無い空港は概算（この空港から出て行った機体だけ）
    機材が普段と違っても候補から外さない（イレギュラー運航こそ撮りたいため）"""
    dest = r["dest"]
    g_d, g_h = iata_geo(dest), AIRPORT_INFO.get(ic)
    if not g_d or not g_h:
        return None
    aps = [a for a in (aps or [ic]) if AIRPORT_INFO.get(a)]
    ship = (state or {}).get("ship") or {}
    arr_dest = dt.datetime.fromtimestamp(r["eta_dest"], tz)
    dest_iata = next((i for i, c in GEO["airports"].items() if c[0] == dest), None)
    names = [ja for ja, i in GEO["ja2iata"].items() if i == dest_iata]
    prefix = (r.get("fl") or "")[:2]
    dist = nm_between(g_d[1], g_d[2], g_h["lat"], g_h["lon"])
    long_haul = dist > 2500
    night = arr_dest.hour >= 19 or arr_dest.hour < 5
    where = (f"{arr_dest.strftime('%m/%d %H:%M').lstrip('0')}に{ap_label(dest)}着"
             + ("（夜間駐機）" if night and not long_haul else "") if arr_dest.timestamp() < (nowts or dt.datetime.now(tz).timestamp())
             else f"いま{ap_label(dest)}へ飛行中（{arr_dest.strftime('%m/%d %H:%M').lstrip('0')}着見込み）")
    ic_name = AIRPORT_INFO.get(ic, {}).get("name", ic)

    # 1) 機材繰りの学習をたどる（最大6便先。羽田・成田のどちらに着く便でも止まる）
    cur, prob, path, seen_ = r.get("fl"), 1.0, [], set()
    after = arr_dest + dt.timedelta(minutes=40)
    for _ in range(6):
        if not cur or cur in seen_:
            break
        seen_.add(cur)
        cands_, total = ship_next(ship, cur)
        if not cands_ or cands_[0][1] < 2 or cands_[0][1] / total < 0.6:
            break
        nxt, n = cands_[0]
        prob *= n / total
        path.append(f"{nxt}（{total}回中{n}回）")
        hits = [(ap2,) + h for ap2 in aps for h in [_tt_arrival(tt, ap2, nxt, after, tz)] if h]
        if hits:
            ap2, tm, row = min(hits, key=lambda x: x[1])
            if ap2 != ic or tm.date() != day:
                return None
            return {"key": f"arr|{row['fl']}", "dir": "arr", "time": row["rev"] or row["time"], "sched": row["time"],
                    "fl": row["fl"], "conf": "有力" if prob >= 0.5 else "傾向", "status": row.get("status", ""),
                    "other": row["ap"], "otherCode": row_code(row), "past": False, "prob": round(prob, 2),
                    "reason": f"{where}。機材繰りの学習: {r.get('fl')} の後は " + " → ".join(path)}
        cur = nxt

    # 2) 行き先から戻る最初の便（時刻表）。羽田・成田の両方を見る
    per_ap = {}
    for ap2 in aps:
        c2, lh2, _ = _first_returns(ap2, r, tt, tz, arr_dest, prefix, dest_iata, names)
        if c2:
            per_ap[ap2] = c2
    cands = per_ap.get(ic) or []
    if cands:
        tm, row = cands[0]
        if tm.date() != day:
            return None
        alt = f"、次点 {cands[1][1]['fl']} {cands[1][0].strftime('%m/%d %H:%M').lstrip('0')}" if len(cands) > 1 else ""
        others = {a2: c2[0] for a2, c2 in per_ap.items() if a2 != ic}
        other_txt = "".join(f"。{AIRPORT_INFO[a2]['name']}に戻る場合は {c[1]['fl']} {c[0].strftime('%m/%d %H:%M').lstrip('0')}"
                            for a2, c in others.items())
        n, total = next_dest_share(ship, (r.get("cs") or "")[:3], dest, ic)
        learned = total >= 4 and n / total >= 0.7
        if learned:
            conf, stat = "有力", f"。{ap_label(dest)}に着いた後、次に{ic_name}へ飛んだのは{total}回中{n}回"
        elif long_haul and not others:
            conf, stat = "有力", ""
        else:
            conf = "傾向"
            stat = (f"。{ap_label(dest)}に着いた後、次に{ic_name}へ飛んだのは{total}回中{n}回" if total
                    else ("。羽田・成田のどちらに戻るかは分かりません" if others
                          else "。別の空港へ向かうこともあります（機材繰りを学習中）"))
        head = "翌朝の最初の便" if night and not long_haul and tm.date() > arr_dest.date() else "折り返しの最短便"
        return {"key": f"arr|{row['fl']}", "dir": "arr", "time": row["rev"] or row["time"], "sched": row["time"],
                "fl": row["fl"], "conf": conf, "status": row.get("status", ""), "other": row["ap"],
                "otherCode": dest_iata, "past": False,
                "reason": f"{where}。{ap_label(dest)}から{ic_name}への{head}{alt}{other_txt}{stat}"}
    # 3) 時刻表にその航空会社の便が無い空港（成田の貨物便など）: この空港から出て行った機体だけ概算
    days_tt = tt.get(ic) or {}
    _, _, earliest = _first_returns(ic, r, tt, tz, arr_dest, prefix or "--", dest_iata, names)
    if earliest is None:
        return None
    has_airline = prefix and any(row["fl"].startswith(prefix) for d_, rows in days_tt.items()
                                 if not d_.startswith("_") for row in rows)
    if has_airline or per_ap or earliest.date() != day or r.get("orig") != ic:
        return None
    return {"key": f"ret|{r['hex']}", "dir": "arr", "time": earliest.strftime("%H:%M"), "approx": True,
            "fl": None, "conf": "傾向", "status": "", "other": ap_label(dest), "otherCode": dest_iata, "past": False,
            "reason": f"{where}。折り返すならこの頃以降"}


def _clusters(w, thr_min):
    peak = max(w) if any(w) else 0
    out = []
    if peak <= 0:
        return out
    thr = max(thr_min, 0.5 * peak)
    h = 5
    while h < 24:
        if w[h] >= thr:
            s = h
            while h + 1 < 24 and w[h + 1] >= thr:
                h += 1
            out.append((s, h, sum(w[s:h + 1])))
        h += 1
    return out


def _describe(clusters, empty):
    if not clusters:
        return empty
    span = sum(e - s + 1 for s, e, _ in clusters)
    if span >= 9 or len(clusters) >= 5:
        return "一日を通して分散"
    top = sorted(sorted(clusters, key=lambda c: -c[2])[:4])
    return "・".join((f"{s}時台" if s == e else f"{s}〜{e}時台") for s, e, _ in top) + "に集中"


def summarize(ic, day, events, hours, now_local):
    counts = {"確定": 0, "有力": 0, "傾向": 0}
    w = [0.0] * 24       # 確定・有力のみ
    wall = [0.0] * 24    # 傾向も含む
    upcoming = [e for e in events if not e.get("past")]
    for e in upcoming:
        counts[e["conf"]] = counts.get(e["conf"], 0) + 1
        if e.get("hoursDetail"):
            # 来訪パターン: その日に来る確率 × その時間帯に来る割合
            for x in e["hoursDetail"]:
                wall[x["h"]] += e.get("ratio", 0.5) * x["n"] / max(1, e.get("visits", 1))
            continue
        for h in e.get("hours") or []:
            v = CONF_W[e["conf"]]
            wall[h] += v
            if e["conf"] != "傾向":
                w[h] += v
    when = _describe(_clusters(w, 0.5), "確定・有力の予定は今のところ無し")
    when_all = _describe(_clusters(wall, 0.3), "注目機の予定は今のところ見つかっていません")
    # 運用の推移
    segs = []
    for r in hours[5:24]:
        if segs and segs[-1][2] == r["cfg"]:
            segs[-1][1] = r["h"]
        else:
            segs.append([r["h"], r["h"], r["cfg"]])
    lab = CONFIG_SHORT.get(ic, {})
    rw = " → ".join(f"{s}〜{e + 1}時 {lab.get(c, c)}" for s, e, c in segs) if len(segs) > 1 else \
        f"終日 {lab.get(segs[0][2], segs[0][2])}" if segs else ""
    # 天気
    sym = {"good": "◎", "fair": "○", "poor": "△", "?": "－"}

    def worst(rng):
        vals = [hours[h]["shoot"] for h in rng]
        for s in ("poor", "fair", "good"):
            if s in vals:
                return s
        return "?"
    weather = f"朝{sym[worst(range(6, 10))]} 昼{sym[worst(range(10, 15))]} 夕{sym[worst(range(15, 19))]}"
    return {"counts": counts, "when": when, "whenAll": when_all, "weights": [round(x, 2) for x in wall],
            "weightsFirm": [round(x, 2) for x in w],
            "runway": rw, "weather": weather, "total": len(upcoming)}


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------
def notify_all(net, cfg, state, plan, now_utc, tz, site_url):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    n = cfg["notify"]
    sent = state.setdefault("notified", {})
    now_local = dt.datetime.fromtimestamp(now_utc.timestamp(), tz)
    today = now_local.date().isoformat()
    quiet = n["quiet_start"] <= now_local.hour or now_local.hour < n["quiet_end"]

    rep = plan.get("report") or {}

    def send(key, title, msg, pri=3, report=False):
        if key in sent:
            return
        actions = None
        if report and rep.get("topic"):
            body = json.dumps({"src": "通知", "key": key, "title": title, "msg": msg[:300], "reason": "通知が間違い"},
                              ensure_ascii=False)
            actions = [{"action": "http", "label": "間違いを報告", "url": f"{rep['server'].rstrip('/')}/{rep['topic']}",
                        "method": "POST", "body": body, "clear": True}]
        try:
            net.notify(n["server"], topic, title, msg, click=site_url, priority=pri, actions=actions)
            sent[key] = now_utc.timestamp()
        except Exception as e:  # noqa: BLE001
            LOG.err("通知", e)

    for which, hour, label in (("tomorrow", n["evening_hour"], "明日"), ("today", n["morning_hour"], "今日")):
        if now_local.hour == hour and f"digest|{which}|{today}" not in sent:
            d = now_local.date() + (dt.timedelta(days=1) if which == "tomorrow" else dt.timedelta())
            lines = []
            for ic, ap in plan["airports"].items():
                day = ap["days"].get(d.isoformat())
                if not day:
                    continue
                s = day["summary"]
                c = s["counts"]
                head = (f"{ap['name']}: 注目{s['total']}件（確定{c['確定']}・有力{c['有力']}・傾向{c['傾向']}）"
                        f"{s['when']}" + (f"／傾向込みなら{s['whenAll']}" if s['whenAll'] != s['when'] else ""))
                picks = [e for e in day["events"] if not e.get("past") and e["conf"] in ("確定", "有力")][:3]
                pk = " / ".join(f"{e.get('time') or ''} {e.get('reg') or e.get('fl') or ''} {e.get('name') or ''}".strip()
                                for e in picks)
                lines.append(head + ("。" + pk if pk else "") + f"。{s['runway']}。天気 {s['weather']}")
            send(f"digest|{which}|{today}", f"{label}（{d.month}/{d.day}）の撮影予報", "\n".join(lines), 3)

    if quiet:
        return
    # 到着が確定した注目機（相手国を出発した時点など）: 1便につき1回。夜間に確定したものは朝に送る
    for ic, ap in plan["airports"].items():
        for d_iso, day in ap["days"].items():
            for e in day["events"]:
                if e["conf"] != "確定" or e.get("status") != "飛行中" or e["cat"] not in NOTABLE:
                    continue
                if (e.get("leftH") or 0) < n["live_minutes"] / 60:
                    continue  # 間近のものは下の「接近」通知に任せる
                dd = dt.date.fromisoformat(d_iso)
                when = ("今日" if dd == now_local.date() else f"{dd.month}/{dd.day}") + f" {e.get('time')}"
                msg = (f"{when}着（{e.get('reason', '')}）\n{e.get('fl') or ''} {e.get('other') or ''}"
                       f"\n{e.get('reg') or ''} {e.get('type') or ''} {e.get('name') or ''}")
                send(f"confirmed|{ic}|{d_iso}|{e['key']}", f"{ap['name']}着が確定: {e.get('reg') or e.get('type') or e.get('fl')}", msg, 4, True)
    horizon = now_utc.timestamp() + n["live_minutes"] * 60
    for ic, ap in plan["airports"].items():
        day = ap["days"].get(today)
        if not day:
            continue
        for e in day["events"]:
            if e["conf"] != "確定" or e.get("past") or e["cat"] not in NOTABLE or not e.get("time"):
                continue
            m = _hhmm_to_min(e["time"])
            if m is None:
                continue
            t = dt.datetime.combine(now_local.date(), dt.time(m // 60, m % 60), tz).timestamp()
            if not (now_utc.timestamp() - 600 <= t <= horizon):
                continue
            mins = int((t - now_utc.timestamp()) // 60)
            if e["dir"] not in ("arr", "dep"):
                continue
            verb = "着" if e["dir"] == "arr" else "発"
            head = "出発準備中・" if e.get("status") == "出発準備中" else ""
            msg = (f"{head}{e['time']}{verb}（約{max(mins, 0)}分後）{e.get('fl') or ''} {e.get('other') or ''}"
                   f"\n{e.get('reg') or ''} {e.get('type') or ''} {e.get('name') or ''}\n滑走路 {e.get('rwy')}（{e.get('rwyConf')}）")
            title = (f"{ap['name']}に来ます: " if e["dir"] == "arr" else f"{ap['name']}から出発: ") + (e.get('reg') or e.get('fl') or "")
            send(f"live|{ic}|{today}|{e['key']}", title, msg, 4, True)
    # 緊急信号（撮影拠点から300nm以内）: 1機・1コードにつき1回
    for x in plan.get("emergencies") or []:
        near_ic = min(x["dist"], key=x["dist"].get)
        if x["dist"][near_ic] > 300:
            continue
        name = AIRPORT_INFO.get(near_ic, {}).get("name", near_ic)
        to = f"{ap_label(x['route'][0])}→{ap_label(x['to'])}" if x.get("route") and len(x["route"]) >= 2 else "行き先不明"
        msg = (f"{x['label']}　{name}から{x['dist'][near_ic]}nm・高度{x['alt'] if x['alt'] is not None else '?'}ft\n"
               f"{x.get('fl') or x.get('cs') or ''} {to}\n{x.get('reg') or ''} {x.get('type') or ''}")
        send(f"emg|{today}|{x['hex']}|{x['label']}", f"緊急信号: {x.get('reg') or x.get('cs') or x['hex']}", msg, 5, True)
    for k in [k for k, v in sent.items() if now_utc.timestamp() - v > 3 * 86400]:
        del sent[k]


# --------------------------------------------------------------------------
# 誤り報告（画面の「違う」ボタン・通知の「間違いを報告」ボタン → ntfy → ここで受け取って保存）
# --------------------------------------------------------------------------
def report_topic(state):
    """報告の受け口。通知用の合言葉とは別の名前にする（ページに載るため）"""
    import hashlib
    base = os.environ.get("NTFY_TOPIC", "").strip()
    if base:
        return "rb-report-" + hashlib.sha256((base + "|report").encode()).hexdigest()[:16]
    if not state.get("report_topic"):
        import secrets
        state["report_topic"] = "rb-report-" + secrets.token_hex(8)
    return state["report_topic"]


def poll_reports(net, cfg, state, data_dir, now_utc, topic):
    """新しい報告を受け取り、そのときの証拠（記録・航跡・経路・実行状況）と一緒に reports.json に残す"""
    path = os.path.join(data_dir, "reports.json")
    saved = load_json(path, [])
    since = state.get("report_since") or "12h"
    try:
        text = net.reports(cfg["notify"]["server"], topic, since)
    except Exception as e:  # noqa: BLE001
        LOG.err("誤り報告の受信", e)
        return saved
    known = {r.get("id") for r in saved}
    new = 0
    for line in (text or "").splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") != "message" or m.get("id") in known:
            continue
        state["report_since"] = m.get("id")
        try:
            body = json.loads(m.get("message") or "{}")
        except ValueError:
            body = {"text": (m.get("message") or "")[:300]}
        if not isinstance(body, dict):
            body = {"text": str(body)[:300]}
        ev = body.get("ev") or {}
        hexes = {x for x in (ev.get("hex"),) if x}
        words = {x for x in (ev.get("fl"), ev.get("reg")) if x}
        # 通知からの報告: キー「live|RJAA|日付|arr|CX87」や題名「成田に来ます: B-LJD」から便名・登録記号を取り出す
        for k in (ev.get("key"), body.get("key")):
            parts = (k or "").split("|")
            if len(parts) >= 2 and parts[-2] in ("arr", "dep", "ret", "pattern", "emg"):
                words.add(parts[-1])
        if ": " in (body.get("title") or ""):
            words.add(body["title"].split(": ", 1)[1].strip())
        words = {w for w in words if len(w) >= 3}
        mv = {k: v for k, v in state.get("mv", {}).items()
              if v.get("hex") in hexes or any(w in (v.get("reg"), v.get("fl"), v.get("cs"), v.get("hex")) for w in words)}
        hexes |= {h for h, tr in state.get("tracks", {}).items() if any(w in x[7:] for x in tr for w in words)}
        hexes |= {v.get("hex") for v in mv.values() if v.get("hex")}
        rec = {"id": m.get("id"), "at": dt.datetime.fromtimestamp(m.get("time") or now_utc.timestamp(),
                                                                  dt.timezone.utc).isoformat(),
               "report": body,
               "evidence": {
                   "mv": mv,
                   "inbound": {k: v for k, v in state.get("inbound", {}).items() if v.get("hex") in hexes},
                   "outbound": {k: v for k, v in state.get("outbound", {}).items() if v.get("hex") in hexes},
                   "tracks": {h: state.get("tracks", {}).get(h) for h in hexes if state.get("tracks", {}).get(h)},
                   "routes": {cs: state.get("routes", {}).get(cs) for cs in
                              {x[7] for h in hexes for x in state.get("tracks", {}).get(h, []) if x[7]}},
                   "runway_live": state.get("runway_live"),
                   "errors": list(LOG.errors), "version": VERSION},
               "status": "未確認"}
        saved.append(rec)
        known.add(rec["id"])
        new += 1
    if new:
        LOG.note(f"誤り報告を{new}件受け取りました")
    del saved[:-200]
    save_json(path, saved, pretty=True)
    return saved


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------
def live_json(cfg, live, watched_air, area_by_ap, now_utc):
    keep = []
    centers = [airport_latlon(ic, {}) for ic in cfg["airports"] if ic in AIRPORT_INFO]
    for a in live:
        if a["lat"] is None:
            continue
        if centers and min(nm_between(c[0], c[1], a["lat"], a["lon"]) for c in centers) > cfg["wide_radius_nm"] + 10:
            continue
        keep.append({"h": a["hex"], "c": a["cs"], "r": a["reg"], "t": a["type"], "la": round(a["lat"], 4),
                     "lo": round(a["lon"], 4), "al": None if a["alt"] is None else int(a["alt"]),
                     "g": a["ground"], "tr": None if a["trk"] is None else int(a["trk"]),
                     "gs": None if a["gs"] is None else int(a["gs"]), "s": a.get("sev"), "w": a.get("why"),
                     "rt": a.get("route")})
    return {"generated": now_utc.isoformat(), "aircraft": keep,
            "center": [airport_latlon(ic, {}) for ic in cfg["airports"] if ic in AIRPORT_INFO]}


OURAIRPORTS_RUNWAYS = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/runways.csv"


def ensure_runways(net, cfg, state):
    """羽田・成田以外の空港は、初回に OurAirports（パブリックドメイン）から滑走路を取得して保存"""
    extra = state.setdefault("extra_runways", {})
    for ic in cfg["airports"]:
        if ic in RUNWAYS:
            continue
        if ic in extra:
            RUNWAYS[ic] = extra[ic]
            continue
        try:
            import csv
            import io
            rows = []
            for r in csv.DictReader(io.StringIO(net.get_text(OURAIRPORTS_RUNWAYS, timeout=60))):
                if r.get("airport_ident") == ic and r.get("closed") == "0" and r.get("le_latitude_deg") and r.get("he_latitude_deg"):
                    rows.append([r["le_ident"], float(r["le_latitude_deg"]), float(r["le_longitude_deg"]),
                                 r["he_ident"], float(r["he_latitude_deg"]), float(r["he_longitude_deg"]),
                                 int(float(r.get("length_ft") or 0))])
            if rows:
                extra[ic] = RUNWAYS[ic] = rows
                LOG.note(f"{ic} の滑走路 {len(rows)}本を取得")
        except Exception as e:  # noqa: BLE001
            LOG.err(f"滑走路データ {ic}", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--fixtures")
    ap.add_argument("--now")
    ap.add_argument("--force-registry", action="store_true")
    args = ap.parse_args()

    user_cfg = load_json(args.config, {})
    cfg_error = None
    if not isinstance(user_cfg, dict):
        cfg_error, user_cfg = "config.json を読み込めませんでした（書式の誤り）。既定値で動作中", {}
    cfg = deep_merge(DEFAULT_CONFIG, user_cfg)
    cfg["airports"] = [a.upper() for a in cfg["airports"] if isinstance(a, str)]
    # 希少機種: 既定の一覧に「足す機種」を加え、「外す機種」を除く
    add = [str(t).upper() for t in (cfg.get("extra_rare_types") or []) if isinstance(t, str)]
    drop = {str(t).upper() for t in (cfg.get("not_rare_types") or []) if isinstance(t, str)}
    cfg["rare_types"] = [t for t in dict.fromkeys(list(cfg["rare_types"]) + add) if t not in drop]
    if not isinstance(cfg.get("airport_unusual_types"), dict):
        cfg["airport_unusual_types"] = DEFAULT_CONFIG["airport_unusual_types"]
    unknown = [a for a in cfg["airports"] if a not in AIRPORT_INFO and not iata_geo(a)]
    if unknown:
        cfg_error = f"未対応の空港コード: {', '.join(unknown)}（ICAOコードで指定してください）"
        cfg["airports"] = [a for a in cfg["airports"] if a not in unknown] or ["RJTT", "RJAA"]
    for ic in cfg["airports"]:
        if ic not in AIRPORT_INFO:
            g = iata_geo(ic)
            AIRPORT_INFO[ic] = {"name": ap_label(ic), "iata": "", "lat": g[1], "lon": g[2], "timetable": None}
    tz = ZoneInfo(cfg["timezone"])
    now_utc = dt.datetime.fromisoformat(args.now).astimezone(dt.timezone.utc) if args.now else \
        dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(tz)
    net = FixtureNet(cfg, args.fixtures) if args.fixtures else Net(cfg)

    data_dir = os.path.join(args.site, "data")
    os.makedirs(data_dir, exist_ok=True)
    state = load_json(os.path.join(data_dir, "state.json"), {})
    ensure_runways(net, cfg, state)
    state.setdefault("runs", 0)
    state["runs"] += 1

    maintain_registry(net, cfg, state, now_local, force=args.force_registry)
    pa = load_plane_alert(net, cfg, state, data_dir, now_utc)
    watch = watchlist(cfg, state)
    tt = fetch_timetable(net, cfg, state, now_local, tz, data_dir)
    wx = parse_weather(net, cfg, state, now_utc, tz)
    live, watched_air, area_by_ap = process_live(net, cfg, state, watch, now_utc, tz, tt, pa)
    state["_watched_air_regs"] = sorted({a["reg"] for a in watched_air if not a["ground"]})
    learn(state, cfg, watch, now_utc, tz, pa)
    rtopic = report_topic(state)
    reports = poll_reports(net, cfg, state, data_dir, now_utc, rtopic)
    plan = build_plan(cfg, state, watch, live, watched_air, tt, wx, now_utc, tz, pa)
    plan["report"] = {"server": cfg["notify"]["server"], "topic": rtopic}
    plan["reports"] = {"count": len(reports), "open": len([r for r in reports if r.get("status") == "未確認"]),
                       "recent": [{"at": r["at"], "reason": r["report"].get("reason"),
                                   "what": (r["report"].get("ev") or {}).get("reg") or (r["report"].get("ev") or {}).get("fl")
                                   or r["report"].get("title")} for r in reports[-5:]][::-1]}
    plan["emergencies"] = state.get("emergencies", [])
    plan["descending"] = state.get("descending", [])

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    site_url = f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if "/" in repo else None
    notify_all(net, cfg, state, plan, now_utc, tz, site_url)

    status = {"generated": now_utc.isoformat(), "version": VERSION, "runs": state["runs"],
              "errors": LOG.errors, "notes": LOG.notes, "config_error": cfg_error, "calls": net.calls,
              "airports": cfg["airports"], "notify": bool(os.environ.get("NTFY_TOPIC")),
              "globalRare": state.get("gtype_n"), "globalVip": state.get("gvip_n"),
              "routesetFail": state.get("routeset_fail", 0), "inbound": len(state.get("inbound", {})),
              "outbound": len(state.get("outbound", {}))}
    plan["status"] = status
    save_json(os.path.join(data_dir, "plan.json"), plan)
    save_json(os.path.join(data_dir, "live.json"), live_json(cfg, live, watched_air, area_by_ap, now_utc))
    save_json(os.path.join(data_dir, "state.json"), state)
    # 画面
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "index.html")
    if os.path.exists(src):
        with open(src, encoding="utf-8") as f:
            html = f.read()
        with open(os.path.join(args.site, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
    open(os.path.join(args.site, ".nojekyll"), "w").close()
    print(f"OK v{VERSION} runs={state['runs']} calls={net.calls} errors={len(LOG.errors)} "
          f"events={sum(len(d['events']) for a in plan['airports'].values() for d in a['days'].values())}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
