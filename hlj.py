import requests

import urllib.parse

import threading

import time

import itertools

import socket

import sys

from typing import Tuple, Any, Dict, Union, Iterable, Optional

from requests.adapters import HTTPAdapter

from urllib3.util.retry import Retry

from base.parser import Parser as BaseParser

# --- IPv6 过滤补丁（避免 IPv6 连接问题）---

original_getaddrinfo = socket.getaddrinfo

def getaddrinfo_ipv4_only(*args):

    try:

        res = original_getaddrinfo(*args)

        ipv4_res = [r for r in res if r[0] == socket.AF_INET]

        if not ipv4_res and res:

            if res[0][0] == socket.AF_INET6:

                return []

            return res

        return ipv4_res

    except Exception:

        raise

# ----------------------------------------------

# ---------------------------- 简单内存缓存 ----------------------------

class SimpleCache:

    def __init__(self, maxsize=128, ttl=60):

        self._cache = {}

        self._maxsize = maxsize

        self._ttl = ttl

        self._lock = threading.Lock()

    def get(self, key):

        with self._lock:

            entry = self._cache.get(key)

            if entry:

                data, expire = entry

                if expire > time.time():

                    return data

                else:

                    del self._cache[key]

            return None

    def set(self, key, value):

        with self._lock:

            if len(self._cache) >= self._maxsize:

                oldest_key = next(iter(self._cache.keys()))

                del self._cache[oldest_key]

            self._cache[key] = (value, time.time() + self._ttl)

# ---------------------------- 代理池负载均衡 ----------------------------

class ProxyPool:

    def __init__(self, proxies: list):

        self._proxies = proxies if proxies else []

        self._lock = threading.Lock()

        self._cycle = itertools.cycle(self._proxies) if self._proxies else None

        self._failures = {p: 0 for p in self._proxies}

        self._max_failures = 3

    def get_next(self) -> Optional[str]:

        if not self._proxies:

            return None

        with self._lock:

            for _ in range(len(self._proxies)):

                proxy = next(self._cycle)

                if self._failures.get(proxy, 0) < self._max_failures:

                    return proxy

        return None

    def mark_failure(self, proxy: str):

        with self._lock:

            if proxy in self._failures:

                self._failures[proxy] += 1

    def mark_success(self, proxy: str):

        with self._lock:

            if proxy in self._failures:

                self._failures[proxy] = 0

# ---------------------------- 优化后的 Parser ----------------------------

class Parser(BaseParser):

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        if not hasattr(self, 'address'):

            self.address = "http://default.proxy.host/path"

        # 代理池配置（请根据实际情况填写）

        self.proxy_pool = ProxyPool(['socks5://111.43.114.137:1080'])   # 默认为空，使用传入的 ip 参数；如需负载均衡请填入代理列表

        # 缓存：m3u8 文件缓存，TTL 5秒适合直播，点播可调大

        self.cache = SimpleCache(maxsize=100, ttl=5)

        # 创建优化的会话

        self.session = self._create_session()

    def _create_session(self) -> requests.Session:

        s = requests.Session()

        retry_strategy = Retry(

            total=1,

            backoff_factor=0.1,

            status_forcelist=[408, 500, 502, 503, 504, 599]

        )

        adapter = HTTPAdapter(

            pool_connections=50,

            pool_maxsize=100,

            max_retries=retry_strategy

        )

        s.mount('http://', adapter)

        s.mount('https://', adapter)

        s.headers.update({

            'Accept': '*/*',

            'Accept-Encoding': 'identity',

            'Connection': 'keep-alive',

            'User-Agent': 'okhttp/3.15',

        })

        requests.packages.urllib3.disable_warnings()

        return s

    def _get_proxy(self, ip: str) -> Dict[str, str]:

        if ip:

            proxy_url = f"socks5://{ip}"

            return {"http": proxy_url, "https": proxy_url}

        pool_proxy = self.proxy_pool.get_next()

        if pool_proxy:

            return {"http": pool_proxy, "https": pool_proxy}

        return {}

    def parse(self, params: Dict[str, str], raw_query_string: str = None) -> Dict[str, str]:

        ip = params.get('ip', '').strip()

        base_url = params.get('u', '').strip()

        if not ip and not self.proxy_pool._proxies:

            return {"error": "缺少代理IP参数且无可用代理池"}

        if not base_url:

            return {"error": "缺少播放URL参数: u"}

        try:

            base_url_decoded = urllib.parse.unquote(base_url)

        except Exception:

            base_url_decoded = base_url

        other_params = {k: v for k, v in params.items() if k not in ['ip', 'u', 'url']}

        target_url = base_url_decoded

        if other_params:

            query_parts = []

            for key, value in other_params.items():

                encoded_value = urllib.parse.quote_plus(value, safe=':/,')

                query_parts.append(f"{key}={encoded_value}")

            query_string = '&'.join(query_parts)

            connector = '&' if '?' in target_url else '?'

            target_url += connector + query_string

        encoded_params = [f"ip={urllib.parse.quote(ip, safe='')}", f"u={urllib.parse.quote(base_url, safe='')}"]

        for key, value in other_params.items():

            encoded_value = urllib.parse.quote_plus(value, safe='')

            encoded_params.append(f"{key}={encoded_value}")

        proxy_url = f"{self.address}?{'&'.join(encoded_params)}"

        return {

            "url": proxy_url,

            "socks5": ip,

            "m3u8_url": target_url

        }

    def proxy(self, url: str, headers: Dict[str, Any]) -> Tuple[Union[bytes, Iterable[bytes]], Dict[str, str]]:

        # 应用 IPv6 过滤补丁

        socket.getaddrinfo = getaddrinfo_ipv4_only

        try:

            parsed_url = urllib.parse.urlparse(url)

            qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)

            ip = qs.get('ip', [''])[0]

            base_url = qs.get('u', [''])[0]

            if not base_url:

                return self._err("缺少必要参数 u", 400)

            target = urllib.parse.unquote(base_url)

            other_params = {}

            for key, values in qs.items():

                if key and key not in ['ip', 'u', 'url']:

                    other_params[key] = urllib.parse.unquote(values[0])

            if other_params:

                query_parts = [f"{k}={urllib.parse.quote_plus(v, safe=':/,')}" for k, v in other_params.items()]

                target += ('&' if '?' in target else '?') + '&'.join(query_parts)

            is_m3u8 = '.m3u8' in target.lower()

            cache_key = f"{target}|{ip}"

            # 缓存检查（仅 m3u8）

            if is_m3u8:

                cached = self.cache.get(cache_key)

                if cached:

                    b = cached.encode('utf-8')

                    h = {'Content-Type': 'application/vnd.apple.mpegurl', 'Content-Length': str(len(b))}

                    return b, self._add_common_headers(h, 200)

            proxy = self._get_proxy(ip)

            used_proxy = proxy.get('http') if proxy else None

            timeout_val = 3 if is_m3u8 else 8

            req_headers = {k: v for k, v in headers.items() if k.lower() in ['range', 'user-agent']}

            req_headers['Accept-Encoding'] = 'identity'

            r = self.session.get(

                target,

                proxies=proxy,

                headers=req_headers,

                timeout=timeout_val,

                verify=False,

                stream=not is_m3u8

            )

            if r.status_code >= 400:

                if used_proxy:

                    self.proxy_pool.mark_failure(used_proxy)

                return self._err(f"目标请求失败: {r.status_code}", r.status_code)

            if used_proxy:

                self.proxy_pool.mark_success(used_proxy)

            if is_m3u8:

                all_params = {k: v[0] for k, v in qs.items() if k != 'url'}

                rewritten = self._rewrite_m3u8(r.text, target, ip, all_params)

                self.cache.set(cache_key, rewritten)

                b = rewritten.encode('utf-8')

                h = {'Content-Type': 'application/vnd.apple.mpegurl', 'Content-Length': str(len(b))}

                return b, self._add_common_headers(h, r.status_code)

            else:

                h = {

                    'Content-Type': r.headers.get('Content-Type', 'video/mp2t'),

                    **{k: v for k, v in r.headers.items() if k.lower() in ['content-length', 'content-range', 'transfer-encoding']}

                }

                return r.iter_content(chunk_size=8192), self._add_common_headers(h, r.status_code)

        except Exception as e:

            if used_proxy:

                self.proxy_pool.mark_failure(used_proxy)

            return self._err(str(e), 500)

        finally:

            socket.getaddrinfo = original_getaddrinfo

    def _rewrite_m3u8(self, content: str, base_url: str, ip: str, original_params: Dict[str, str]) -> str:

        if not content.strip():

            return content

        url_parts = urllib.parse.urlparse(base_url)

        base_dir = base_url.rsplit('/', 1)[0] + '/'

        base_root = f"{url_parts.scheme}://{url_parts.netloc}"

        lines = []

        for l in content.splitlines():

            l = l.strip()

            if not l:

                continue

            if l.startswith("#"):

                if l.upper().startswith('#EXT-X-KEY'):

                    parts = l.split(',')

                    new_parts = []

                    for part in parts:

                        if part.upper().startswith('URI='):

                            uri_val = part[4:].strip().strip('"')

                            abs_url = urllib.parse.urljoin(base_dir, uri_val) if not uri_val.startswith("http") else uri_val

                            if uri_val.startswith("/"):

                                abs_url = f"{base_root}{uri_val}"

                            p_parts = [f"ip={urllib.parse.quote(ip)}", f"u={urllib.parse.quote_plus(abs_url, safe='')}"]

                            for k, v in original_params.items():

                                if k not in ['ip', 'u']:

                                    p_parts.append(f"{k}={urllib.parse.quote_plus(v, safe='')}")

                            new_parts.append(f'URI="{self.address}?{"&".join(p_parts)}"')

                        else:

                            new_parts.append(part)

                    lines.append(','.join(new_parts))

                else:

                    lines.append(l)

                continue

            full = urllib.parse.urljoin(base_dir, l) if not l.startswith("http") else l

            if l.startswith("/"):

                full = f"{base_root}{l}"

            p_parts = [f"ip={urllib.parse.quote(ip)}", f"u={urllib.parse.quote_plus(full, safe='')}"]

            for k, v in original_params.items():

                if k not in ['ip', 'u']:

                    p_parts.append(f"{k}={urllib.parse.quote_plus(v, safe='')}")

            lines.append(f"{self.address}?{'&'.join(p_parts)}")

        return "\n".join(lines)

    def _add_common_headers(self, headers: Dict[str, str], status_code: int) -> Dict[str, str]:

        headers.update({

            "Access-Control-Allow-Origin": "*",

            "X-Proxy-Status-Code": str(status_code),

            "Cache-Control": "no-cache"

        })

        return headers

    def _err(self, msg: str, status_code: int = 500) -> Tuple[bytes, Dict[str, str]]:

        b = f"[Error {status_code}] {msg}".encode('utf-8')

        return b, self._add_common_headers({"Content-Type": "text/plain", "Content-Length": str(len(b))}, status_code)

    def stop(self):

        if hasattr(self, 'session'):

            self.session.close()

    def __del__(self):

        self.stop()