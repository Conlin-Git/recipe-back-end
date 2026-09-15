"""口令传输加密：RSA-OAEP(SHA-256)。

前端用公钥加密密码后再传输，服务端用私钥解密后再走 bcrypt 校验/入库，
避免明文密码出现在请求体里（被网关/代理日志记录、或 TLS 终止点后裸奔）。

注意：这只是 HTTPS 的补充而非替代——公钥下发本身需要 TLS 防中间人替换，
生产环境必须启用 HTTPS。

私钥配置：.env 里 RSA_PRIVATE_KEY（PEM 文本，换行用 \\n 转义）。
未配置时启动生成临时密钥（仅开发用，重启后前端缓存的公钥即失效）。
"""
import base64
import binascii

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import settings
from app.core.exceptions import BusinessException

_OAEP = padding.OAEP(
    mgf=padding.MGF1(algorithm=hashes.SHA256()),
    algorithm=hashes.SHA256(),
    label=None,
)


def _load_private_key() -> rsa.RSAPrivateKey:
    if settings.RSA_PRIVATE_KEY:
        # 兼容 \\n 双重转义（某些部署平台的 env 注入会再转义一层）
        pem = (
            settings.RSA_PRIVATE_KEY.replace("\\\\n", "\n").replace("\\n", "\n").encode("utf-8")
        )
        return serialization.load_pem_private_key(pem, password=None)  # type: ignore[return-value]
    print("⚠️ 未配置 RSA_PRIVATE_KEY，生成临时密钥（仅开发环境可用，重启后旧密文无法解密）")
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


_private_key = _load_private_key()


def public_key_pem() -> str:
    """SPKI 格式 PEM 公钥，前端 WebCrypto importKey('spki') 可直接用。"""
    return _private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def decrypt_password(ciphertext_b64: str) -> str:
    """解密前端 RSA-OAEP 加密的密码；密文非法统一报 400，不泄露具体原因。"""
    try:
        plaintext = _private_key.decrypt(base64.b64decode(ciphertext_b64, validate=True), _OAEP)
        return plaintext.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise BusinessException(400, "密码密文无效，请刷新后重试")
