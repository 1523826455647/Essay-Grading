"""OCR 文字识别接口

POST /api/ocr
  表单字段：
    - image: 图片文件（JPG/PNG/WebP/BMP）
    - handwriting: 可选，"true" 表示手写体（默认 true）

响应：
    { "data": { "text": "...", "length": N, "provider": "baidu" } }
"""

from flask import Blueprint, request
import os
import tempfile
import logging

from PIL import Image

from src.api.utils import api_success, api_error, token_required
from src.services.ocr_service import get_ocr_service

logger = logging.getLogger(__name__)

ocr_bp = Blueprint('ocr', __name__, url_prefix='/api/ocr')

ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/bmp'}
MAX_SIZE = 10 * 1024 * 1024  # 10MB


@ocr_bp.route('', methods=['POST'])
@token_required
def ocr_recognize(current_user):
    """OCR 图片文字识别"""
    if 'image' not in request.files:
        return api_error("请上传图片", 400)

    file = request.files['image']
    if not file or not file.filename:
        return api_error("请选择图片文件", 400)

    if file.content_type not in ALLOWED_TYPES:
        return api_error("仅支持 JPG/PNG/WebP/BMP 格式", 400)

    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    if file_size > MAX_SIZE:
        return api_error("图片大小不能超过 10MB", 400)

    handwriting = request.form.get('handwriting', 'true').lower() in ('true', '1', 'yes')

    tmp_path = None
    try:
        ext = _get_extension(file.content_type)
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        try:
            image = Image.open(tmp_path)
            image.load()
        except Exception:
            return api_error("图片文件损坏或格式不支持，请重新上传", 400)

        result = get_ocr_service().recognize(image, handwriting=handwriting)

        if not result.text:
            return api_error("未能识别出文字，请尝试光线更好、更清晰的图片", 400)

        return api_success({
            'text': result.text,
            'length': len(result.text),
            'provider': result.provider,
        })

    except Exception as e:
        logger.exception('OCR failed')
        return api_error(f"识别失败: {str(e)}", 500)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _get_extension(content_type):
    extensions = {
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'image/bmp': '.bmp',
    }
    return extensions.get(content_type, '.jpg')