"""Collect the native libraries needed for password-login RSA encryption."""

from pathlib import Path

from PyInstaller.utils.hooks import get_module_file_attribute

binaries = []
for package, stem in (
    ("Crypto.Cipher", "_Salsa20"),
    ("Crypto.Cipher", "_pkcs1_decode"),
    ("Crypto.Cipher", "_raw_aes"),
    ("Crypto.Cipher", "_raw_aesni"),
    ("Crypto.Cipher", "_raw_des"),
    ("Crypto.Cipher", "_raw_des3"),
    ("Crypto.Hash", "_BLAKE2s"),
    ("Crypto.Hash", "_MD5"),
    ("Crypto.Hash", "_SHA1"),
    ("Crypto.Hash", "_SHA256"),
    ("Crypto.Math", "_modexp"),
    ("Crypto.Protocol", "_scrypt"),
    ("Crypto.Util", "_cpuid_c"),
    ("Crypto.Util", "_strxor"),
):
    directory = Path(get_module_file_attribute(package)).parent
    matches = [p for p in directory.glob(stem + "*.pyd") if p.name.split(".")[0] == stem]
    if len(matches) != 1:
        raise RuntimeError("密码登录原生运行库缺失或存在歧义")
    binaries.append((str(matches[0]), package.replace(".", "/")))
