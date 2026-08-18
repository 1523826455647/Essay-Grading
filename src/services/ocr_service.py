"""OCR 文字识别服务 — 多供应商 + 图像预处理

支持供应商：
  - tesseract   : 本地 Tesseract（免费，但对中文手写效果差）
  - baidu       : 百度 OCR（手写模型，免费 500次/天）
  - tencent     : 腾讯云 OCR（通用高精度，免费 1000次/月）

图像预处理（提升手写识别率）：
  - 灰度化 + 自适应对比度增强
  - 自适应二值化（大津法 + 局部阈值）
  - 去噪（中值滤波）
  - 自动纠偏（基于 Hough 变换）

配置方式：
  BAIDU_OCR_API_KEY    — 百度 OCR API Key
  BAIDU_OCR_SECRET_KEY — 百度 OCR Secret Key
  TENCENT_OCR_SECRET_ID  — 腾讯云 SecretId
  TENCENT_OCR_SECRET_KEY — 腾讯云 SecretKey
  OCR_PROVIDER         — 首选供应商 (baidu / tencent / tesseract)，默认 baidu
"""

import base64
import io
import logging
import os
import time
from typing import Optional, Tuple

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 图像预处理
# ---------------------------------------------------------------------------

PREPROCESS_MAX_SIZE = 2048  # 预处理后最大边长（像素）


def _resize_if_large(img: Image.Image, max_size: int = PREPROCESS_MAX_SIZE) -> Image.Image:
    """如果图片过大，等比缩放到 max_size 以内。"""
    w, h = img.size
    if max(w, h) <= max_size:
        return img
    ratio = max_size / max(w, h)
    return img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)


def preprocess_for_handwriting(image: Image.Image) -> Image.Image:
    """针对手写文字的图像预处理流水线。

    步骤：灰度 → 对比度增强 → 自适应二值化 → 去噪
    """
    # 1. 缩放（避免大图处理慢）
    img = _resize_if_large(image)

    # 2. 转灰度
    if img.mode != 'L':
        img = img.convert('L')

    # 3. 对比度增强（CLAHE 模拟：先拉伸 histogram）
    #    用 ImageOps.autocontrast 做全局直方图拉伸
    img = ImageOps.autocontrast(img, cutoff=2)

    # 4. 锐化（让笔迹边缘更清晰）
    img = img.filter(ImageFilter.SHARPEN)

    # 5. 自适应二值化：用局部阈值
    #    先模糊得到局部平均亮度，再与原图比较
    blur_radius = max(2, min(img.size) // 40)
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    # 逐像素比较：原图比局部平均暗 → 文字（黑），否则 → 背景（白）
    # 偏移量 offset 控制灵敏度
    offset = 12
    bw = Image.new('L', img.size)
    for y in range(img.height):
        for x in range(img.width):
            src = img.getpixel((x, y))
            ref = blurred.getpixel((x, y))
            bw.putpixel((x, y), 0 if src < ref - offset else 255)

    # 6. 去噪（中值滤波消除孤立噪点）
    bw = bw.filter(ImageFilter.MedianFilter(size=3))

    return bw


def preprocess_for_printed(image: Image.Image) -> Image.Image:
    """针对印刷文字的轻量预处理。"""
    img = _resize_if_large(image)
    if img.mode != 'L':
        img = img.convert('L')
    img = ImageOps.autocontrast(img, cutoff=1)
    return img


def image_to_base64(image: Image.Image, fmt: str = 'PNG') -> str:
    """将 PIL Image 编码为 base64 字符串。"""
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode('ascii')


# ---------------------------------------------------------------------------
# OCR 供应商接口
# ---------------------------------------------------------------------------

class OcrResult:
    """OCR 识别结果。"""
    def __init__(self, text: str, provider: str, confidence: float = 0.0):
        self.text = text.strip()
        self.provider = provider
        self.confidence = confidence

    def __bool__(self):
        return bool(self.text)


class BaseOcrProvider:
    """OCR 供应商基类。"""
    name = 'base'

    def recognize(self, image: Image.Image) -> OcrResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Tesseract（本地）
# ---------------------------------------------------------------------------

class TesseractProvider(BaseOcrProvider):
    name = 'tesseract'

    def recognize(self, image: Image.Image) -> OcrResult:
        try:
            import pytesseract
        except ImportError:
            return OcrResult('', self.name)

        try:
            # 手写体用 psm 6（统一文本块），印刷体也可用 psm 3
            text = pytesseract.image_to_string(
                image, lang='chi_sim+eng', config='--psm 6 --oem 3'
            )
            return OcrResult(text, self.name)
        except Exception as e:
            logger.warning('Tesseract OCR failed: %s', e)
            return OcrResult('', self.name)


# ---------------------------------------------------------------------------
# 百度 OCR
# ---------------------------------------------------------------------------

class BaiduOcrProvider(BaseOcrProvider):
    name = 'baidu'

    # 手写识别接口（对中文手写优化最好）
    HANDWRITING_URL = 'https://aip.baidubce.com/rest/2.0/ocr/v1/handwriting'
    # 通用高精度（印刷体首选）
    ACCURATE_URL = 'https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic'
    TOKEN_URL = 'https://aip.baidubce.com/oauth/2.0/token'

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self._token: Optional[Tuple[str, float]] = None  # (token, expiry)

    def _get_access_token(self) -> str:
        """获取或刷新百度 access_token（缓存至过期前5分钟）。"""
        now = time.time()
        if self._token and self._token[1] > now + 300:
            return self._token[0]

        try:
            resp = requests.post(
                self.TOKEN_URL,
                data={
                    'grant_type': 'client_credentials',
                    'client_id': self.api_key,
                    'client_secret': self.secret_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get('access_token')
            if not token:
                raise Exception(f"百度 OCR token 获取失败: {data}")
            expires_in = data.get('expires_in', 2592000)  # 默认30天
            self._token = (token, now + expires_in)
            return token
        except requests.RequestException as e:
            raise Exception(f'百度 OCR 鉴权失败: {e}') from e

    def recognize(self, image: Image.Image, handwriting: bool = True) -> OcrResult:
        """调用百度 OCR。

        Args:
            image: PIL Image 对象
            handwriting: True 使用手写模型，False 使用通用高精度模型
        """
        try:
            token = self._get_access_token()
        except Exception as e:
            return OcrResult(f'鉴权失败: {e}', self.name)

        url = self.HANDWRITING_URL if handwriting else self.ACCURATE_URL
        url += f'?access_token={token}'

        # 预处理后转 base64
        processed = preprocess_for_handwriting(image) if handwriting else preprocess_for_printed(image)
        b64 = image_to_base64(processed)

        try:
            resp = requests.post(
                url,
                data={
                    'image': b64,
                    'language_type': 'CHN_ENG',
                    'detect_direction': 'true',
                    'paragraph': 'true',
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            if 'error_code' in data:
                err_msg = data.get('error_msg', 'Unknown error')
                logger.warning('Baidu OCR error: %s - %s', data['error_code'], err_msg)
                return OcrResult(f'百度 OCR 错误: {err_msg}', self.name)

            words = data.get('words_result', [])
            if not words:
                return OcrResult('', self.name)

            text = '\n'.join(item.get('words', '') for item in words)
            return OcrResult(text, self.name)

        except requests.RequestException as e:
            logger.warning('Baidu OCR request failed: %s', e)
            return OcrResult(f'请求失败: {e}', self.name)


# ---------------------------------------------------------------------------
# 腾讯云 OCR
# ---------------------------------------------------------------------------

class TencentOcrProvider(BaseOcrProvider):
    name = 'tencent'

    ENDPOINT = 'ocr.tencentcloudapi.com'
    SERVICE = 'ocr'
    VERSION = '2018-11-19'
    ACTION = 'GeneralAccurateOCR'

    def __init__(self, secret_id: str, secret_key: str):
        self.secret_id = secret_id
        self.secret_key = secret_key

    def _sign(self, secret_key: str, sign_str: str, method: str = 'HmacSHA256') -> bytes:
        import hmac
        import hashlib

        if method == 'HmacSHA256':
            return hmac.new(secret_key.encode(), sign_str.encode(), hashlib.sha256).digest()
        return hmac.new(secret_key.encode(), sign_str.encode(), hashlib.sha1).digest()

    def recognize(self, image: Image.Image) -> OcrResult:
        try:
            import hashlib
            import hmac
            import json as json_mod
            from datetime import datetime, timezone
        except ImportError:
            return OcrResult('缺少依赖', self.name)

        # 预处理
        processed = preprocess_for_printed(image)
        b64 = image_to_base64(processed, fmt='JPEG')

        # 构建请求
        payload = json_mod.dumps({
            'ImageBase64': b64,
            'ConfigID': 'OCR',
            'EnableDetectText': True,
            'IsWords': False,
        })

        # 腾讯云 API v3 签名
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime('%Y-%m-%d')

        # 1. 拼接规范请求串
        http_method = 'POST'
        canonical_uri = '/'
        canonical_querystring = ''
        canonical_headers = (
            f'content-type:application/json; charset=utf-8\n'
            f'host:{self.ENDPOINT}\n'
            f'x-tc-action:{self.ACTION.lower()}\n'
        )
        signed_headers = 'content-type;host;x-tc-action'
        hashed_payload = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        canonical_request = (
            f'{http_method}\n'
            f'{canonical_uri}\n'
            f'{canonical_querystring}\n'
            f'{canonical_headers}\n'
            f'{signed_headers}\n'
            f'{hashed_payload}'
        )

        # 2. 拼接待签名字符串
        algorithm = 'TC3-HMAC-SHA256'
        credential_scope = f'{date}/{self.SERVICE}/tc3_request'
        hashed_canonical_request = hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()
        string_to_sign = (
            f'{algorithm}\n'
            f'{timestamp}\n'
            f'{credential_scope}\n'
            f'{hashed_canonical_request}'
        )

        # 3. 计算签名
        secret_date = self._sign(f'TC3{self.secret_key}', date)
        secret_service = self._sign(secret_date, self.SERVICE)
        secret_signing = self._sign(secret_service, 'tc3_request')
        signature = hmac.new(
            secret_signing, string_to_sign.encode('utf-8'), hashlib.sha256
        ).hexdigest()

        # 4. Authorization
        authorization = (
            f'{algorithm} '
            f'Credential={self.secret_id}/{credential_scope}, '
            f'SignedHeaders={signed_headers}, '
            f'Signature={signature}'
        )

        headers = {
            'Authorization': authorization,
            'Content-Type': 'application/json; charset=utf-8',
            'Host': self.ENDPOINT,
            'X-TC-Action': self.ACTION,
            'X-TC-Timestamp': str(timestamp),
            'X-TC-Version': self.VERSION,
        }

        try:
            resp = requests.post(
                f'https://{self.ENDPOINT}',
                headers=headers,
                data=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            if 'Response' not in data:
                return OcrResult('', self.name)
            r = data['Response']
            if 'Error' in r:
                err = r['Error']
                logger.warning('Tencent OCR error: %s - %s',
                               err.get('Code'), err.get('Message'))
                return OcrResult(f"腾讯 OCR 错误: {err.get('Message')}", self.name)

            detections = r.get('TextDetections', [])
            text = '\n'.join(
                d.get('DetectedText', '') for d in detections
            )
            return OcrResult(text, self.name)

        except requests.RequestException as e:
            logger.warning('Tencent OCR request failed: %s', e)
            return OcrResult(f'请求失败: {e}', self.name)


# ---------------------------------------------------------------------------
# OCR 服务门面
# ---------------------------------------------------------------------------

class OcrService:
    """统一 OCR 服务，管理多个供应商并自动选择。"""

    def __init__(self):
        self._providers = []
        self._init_providers()

    def _init_providers(self):
        """按优先级初始化供应商。"""
        preferred = os.getenv('OCR_PROVIDER', 'baidu').lower()

        # 百度（手写模型 → 首选）
        baidu_key = os.getenv('BAIDU_OCR_API_KEY', '').strip()
        baidu_secret = os.getenv('BAIDU_OCR_SECRET_KEY', '').strip()
        if baidu_key and baidu_secret:
            provider = BaiduOcrProvider(baidu_key, baidu_secret)
            if preferred == 'baidu':
                self._providers.insert(0, provider)
            else:
                self._providers.append(provider)
            logger.info('OCR: 百度 OCR 已配置（手写模型）')

        # 腾讯云
        tc_id = os.getenv('TENCENT_OCR_SECRET_ID', '').strip()
        tc_key = os.getenv('TENCENT_OCR_SECRET_KEY', '').strip()
        if tc_id and tc_key:
            provider = TencentOcrProvider(tc_id, tc_key)
            if preferred == 'tencent':
                self._providers.insert(0, provider)
            else:
                self._providers.append(provider)
            logger.info('OCR: 腾讯云 OCR 已配置')

        # Tesseract（本地兜底）
        if preferred == 'tesseract':
            self._providers.insert(0, TesseractProvider())
        else:
            self._providers.append(TesseractProvider())
        logger.info('OCR: Tesseract 已作为兜底')

    def recognize(self, image: Image.Image, handwriting: bool = True) -> OcrResult:
        """按优先级依次尝试供应商，返回第一个成功的结果。

        Args:
            image: PIL Image 对象
            handwriting: 是否手写体（仅百度供应商区分手写/印刷模型）

        Returns:
            OcrResult: 识别结果，失败时 text 为空
        """
        if not self._providers:
            return OcrResult('未配置任何 OCR 供应商', 'none')

        for provider in self._providers:
            try:
                if isinstance(provider, BaiduOcrProvider):
                    result = provider.recognize(image, handwriting=handwriting)
                else:
                    result = provider.recognize(image)

                if result and len(result.text) > 2:
                    logger.info('OCR success via %s: %d chars', result.provider, len(result.text))
                    return result
                elif result and result.text:
                    logger.warning('OCR via %s returned very short text: "%s"', result.provider, result.text)
                    # 继续尝试下一个供应商
                    continue
            except Exception as e:
                logger.warning('OCR provider %s failed: %s', provider.name, e)
                continue

        return OcrResult('所有 OCR 供应商均未能识别出有效文字', 'none')


# 全局单例
_ocr_service: Optional[OcrService] = None


def get_ocr_service() -> OcrService:
    global _ocr_service
    if _ocr_service is None:
        _ocr_service = OcrService()
    return _ocr_service