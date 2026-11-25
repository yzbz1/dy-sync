from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import yaml


@dataclass
class AntiRiskConfig:
    download_delay_min: float = 1.5
    download_delay_max: float = 3.5
    scroll_delay_min: float = 2.0
    scroll_delay_max: float = 4.5
    batch_cooldown_every: int = 15
    batch_cooldown_min: float = 15.0
    batch_cooldown_max: float = 30.0
    creator_delay_min: float = 5.0
    creator_delay_max: float = 12.0
    circuit_breaker_failures: int = 5
    rate_limit_backoff_base: float = 10.0


@dataclass
class AppConfig:
    download_dir: Path = Path("./downloads")
    data_dir: Path = Path("./data")
    concurrency: int = 2
    rate_limit: float = 1.5
    retry_times: int = 3
    timeout_seconds: int = 120
    redownload_missing: bool = True
    incremental_stop_threshold: int = 5
    max_scroll_pages: int = 50
    headless: bool = False
    browser_channel: str = ""
    filename_template: str = "{date}_{title}_{id}"
    max_title_length: int = 50
    anti_risk: AntiRiskConfig = field(default_factory=AntiRiskConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dy_sync.db"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser_profile"

    def ensure_dirs(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)


def _clamp_range(lo: float, hi: float, floor: float = 0.0) -> tuple[float, float]:
    lo = max(floor, lo)
    hi = max(lo, hi)
    return lo, hi


def load_config(config_path: str | Path = "config.yml") -> AppConfig:
    path = Path(config_path)
    base_dir = path.parent.resolve() if path.exists() else Path.cwd()
    raw: dict = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    def _resolve(val: str | None, default: str) -> Path:
        p = Path(val or default)
        return p if p.is_absolute() else (base_dir / p).resolve()

    ar_raw = raw.get("anti_risk") or {}
    dl_min, dl_max = _clamp_range(
        float(ar_raw.get("download_delay_min", 1.5)),
        float(ar_raw.get("download_delay_max", 3.5)),
    )
    sc_min, sc_max = _clamp_range(
        float(ar_raw.get("scroll_delay_min", raw.get("scroll_delay_seconds", 2.0))),
        float(ar_raw.get("scroll_delay_max", 4.5)),
        floor=0.5,
    )
    bc_min, bc_max = _clamp_range(
        float(ar_raw.get("batch_cooldown_min", 15.0)),
        float(ar_raw.get("batch_cooldown_max", 30.0)),
    )
    cr_min, cr_max = _clamp_range(
        float(ar_raw.get("creator_delay_min", 5.0)),
        float(ar_raw.get("creator_delay_max", 12.0)),
    )

    anti_risk = AntiRiskConfig(
        download_delay_min=dl_min,
        download_delay_max=dl_max,
        scroll_delay_min=sc_min,
        scroll_delay_max=sc_max,
        batch_cooldown_every=max(0, int(ar_raw.get("batch_cooldown_every", 15))),
        batch_cooldown_min=bc_min,
        batch_cooldown_max=bc_max,
        creator_delay_min=cr_min,
        creator_delay_max=cr_max,
        circuit_breaker_failures=max(
            1, int(ar_raw.get("circuit_breaker_failures", 5))
        ),
        rate_limit_backoff_base=max(
            1.0, float(ar_raw.get("rate_limit_backoff_base", 10.0))
        ),
    )

    cfg = AppConfig(
        download_dir=_resolve(raw.get("download_dir"), "./downloads"),
        data_dir=_resolve(raw.get("data_dir"), "./data"),
        concurrency=max(1, int(raw.get("concurrency", 2))),
        rate_limit=max(0.2, float(raw.get("rate_limit", 1.5))),
        retry_times=max(1, int(raw.get("retry_times", 3))),
        timeout_seconds=max(10, int(raw.get("timeout_seconds", 120))),
        redownload_missing=bool(raw.get("redownload_missing", True)),
        incremental_stop_threshold=max(
            1, int(raw.get("incremental_stop_threshold", 5))
        ),
        max_scroll_pages=max(0, int(raw.get("max_scroll_pages", 50))),
        headless=bool(raw.get("headless", False)),
        browser_channel=str(raw.get("browser_channel", "") or ""),
        filename_template=str(raw.get("filename_template", "{date}_{title}_{id}")),
        max_title_length=max(10, int(raw.get("max_title_length", 50))),
        anti_risk=anti_risk,
    )
    cfg.ensure_dirs()
    return cfg
