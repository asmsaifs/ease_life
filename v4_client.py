import hashlib
import hmac
import json
import time
import uuid

import requests

PRODUCT_KEY = "45c8bd2a-e70"
PRODUCT_SECRET = "qqg5C36lYBlDAQvp3XL6"
SOUL = "https://soul.ehomeease.com"


class EaseLifeAuthError(Exception):
    pass


def md5_arcsoft(password):
    return hashlib.md5(("Arcsoft_" + password).encode()).hexdigest()


def generate_sign(access_key, ts_ms, path, secret, body, nonce=""):
    a = access_key + "|" + ts_ms + "|" + path
    if nonce:
        a += "|" + nonce
    i = hmac.new(a.encode(), secret.encode(), hashlib.sha256).digest()
    body_sha = hashlib.sha256(body.encode()).digest()
    return hmac.new(i, body_sha, hashlib.sha256).hexdigest()


def post(host, path, data, token="", session=None):
    ts = str(int(time.time() * 1000))
    body = json.dumps(data, separators=(",", ":"))
    nonce = uuid.uuid4().hex
    headers = {
        "User-Agent": "okhttp/4.9.2",
        "Request-Version": "1",
        "Accept-Time": ts,
        "Accept-AccessKey": PRODUCT_KEY,
        "Accept-Nonce": nonce,
        "Accept-Sign": generate_sign(PRODUCT_KEY, ts, path, PRODUCT_SECRET, body, nonce),
        "Content-Type": "application/json; charset=utf-8",
    }
    if token:
        headers["userToken"] = token
    http = session or requests
    return http.post(host + path, headers=headers, data=body, timeout=25)


def login(email, password, device_id=None, session=None, host=SOUL):
    data = {
        "data": {
            "account": email,
            "accountType": "email",
            "deviceId": device_id or str(uuid.uuid4()),
            "password": md5_arcsoft(password),
            "passwordTypeEnum": "enc1",
            "loc": "en_US",
        }
    }
    r = post(host, "/oauth/user/login", data, session=session)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 200:
        raise EaseLifeAuthError(payload.get("msg", "login failed"))
    return payload["data"]


def device_list(token, page_size=50, session=None, host=SOUL):
    body = {"data": {"pageSize": page_size, "settingPaths": [], "supportPaths": [], "attributeIdList": []}}
    r = post(host, "/sclient/compatible/device/list", body, token=token, session=session)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 200:
        raise EaseLifeAuthError(payload.get("msg", "device list failed"))
    return payload["data"]["deviceList"]


def refresh_token(refresh, session=None, host=SOUL):
    r = post(host, "/oauth/token/refresh", {"data": {"refreshToken": refresh}}, session=session)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 200:
        raise EaseLifeAuthError(payload.get("msg", "refresh failed"))
    return payload["data"]


def thumbnail_bytes(device, width=320, session=None):
    url = device["thumbnailUrlList"][0]["url"]
    if "width=" not in url:
        url += "&width=%d" % width
    http = session or requests
    r = http.get(url, timeout=25)
    r.raise_for_status()
    return r.content
