"""给跨机链路（樱花穿透）生成一对**自签** TLS 证书。

    python tools/make_tls_cert.py                 # → data/tls/{cert.pem, key.pem}
    python tools/make_tls_cert.py --cn frp.xx.cc  # 指定证书里的名字
    python tools/make_tls_cert.py --force         # 覆盖已有

为什么自签就够：跨机那一段是**我们自己的**两台机器 + 樱花穿透的原始 TCP 端口，
两边都由本项目控制 —— 客户端侧把 `transport/link/tls/verify_name_on_connect`
关掉（用户原话："证书没有验证无所谓"），所以证书有效与否不影响连接，
自签省掉一切域名/CA 麻烦。

⚠️ 这只是"链路加密"，**不是**身份认证：真要防中间人，就得给两边都配
`root_ca_certificate` + `enable_mtls = true`（见 start.ps1 里的注释）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import ipaddress
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core.config import force_utf8  # noqa: E402

DEFAULT_DIR = ROOT / "data" / "tls"


def make_cert(out_dir: Path, *, cn: str = "webdg.local", days: int = 3650) -> tuple[Path, Path]:
    """生成自签证书 + 私钥（PEM），返回 (cert_path, key_path)。"""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise SystemExit(f"需要 cryptography（requirements.txt 里有）：{exc}") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(_dt.UTC)
    # 名字/IP 都塞进 SAN：即使哪天开了校验也不会立刻炸（IP 用通配 + 本机常见地址）
    sans: list[x509.GeneralName] = [
        x509.DNSName(cn),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("0.0.0.0")),
    ]
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = out_dir / "cert.pem"
    key_path = out_dir / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="生成跨机链路用的自签 TLS 证书")
    ap.add_argument("--cn", default="webdg.local", help="证书里的名字（樱花域名也可以填这里）")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="输出目录")
    ap.add_argument("--days", type=int, default=3650, help="有效期（天）")
    ap.add_argument("--force", action="store_true", help="已存在也重新生成")
    args = ap.parse_args(argv)

    out = Path(args.dir)
    cert, key = out / "cert.pem", out / "key.pem"
    if cert.exists() and key.exists() and not args.force:
        print(f"已存在，跳过：{cert} / {key}（要重签就加 --force）")
        return 0
    c, k = make_cert(out, cn=args.cn, days=args.days)
    print(f"自签证书已生成：\n  cert = {c}\n  key  = {k}\n  CN   = {args.cn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
