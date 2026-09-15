from pydantic import BaseModel, Field

# 密码字段传输的是 RSA-OAEP 加密后的 base64 密文（2048 位密钥密文固定 344 字符），
# 明文长度校验在服务端解密后进行
PASSWORD_CIPHERTEXT_MAX = 512


class RegisterIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=1, max_length=PASSWORD_CIPHERTEXT_MAX)
    email: str | None = None


class LoginIn(BaseModel):
    username: str
    password: str = Field(min_length=1, max_length=PASSWORD_CIPHERTEXT_MAX)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PublicKeyOut(BaseModel):
    public_key: str
