"""Images for the email and the briefing PDF, drawn with matplotlib in the LTM palette.

- hero_wave: the coral particle "terrain" on black used on ltm.com, as a decorative banner.
- sector_pie / city_bars: data charts (PNG bytes).
"""
import io
from collections import Counter
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from . import ltm  # noqa: E402
from .models import GCC  # noqa: E402

CHART_COLORS = [ltm.CORAL, ltm.INK, "#8C939D", "#F5A08F", "#3D434C", "#C9CDD3", "#B8321C"]


def _png(fig, facecolor: str) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=facecolor, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    return buf.getvalue()


def hero_wave(width_in: float = 12.0, height_in: float = 2.6, seed: int = 7) -> bytes:
    """Black banner with a perspective field of coral particles rising to the right (decorative)."""
    rng = np.random.default_rng(seed)
    n = 26000
    x = rng.uniform(0, 1, n)
    depth = rng.uniform(0, 1, n) ** 1.6            # more particles near the viewer (bottom)
    terrain = 0.22 * np.sin(6.0 * x + 1.3) + 0.15 * np.sin(13 * x * (0.6 + depth)) + 0.35 * x
    y = 0.08 + 0.55 * depth + terrain * (0.4 + 0.6 * depth) * 0.5
    y += rng.normal(0, 0.012, n)
    keep = (y > 0) & (y < 1) & (rng.uniform(0, 1, n) < 0.25 + 0.75 * x)   # denser to the right
    size = (0.3 + 3.2 * depth[keep] ** 2)
    alpha = np.clip(0.15 + 0.8 * depth[keep], 0, 1)
    colors = np.zeros((keep.sum(), 4))
    colors[:, :3] = matplotlib.colors.to_rgb(ltm.CORAL)
    colors[:, 3] = alpha
    fig = plt.figure(figsize=(width_in, height_in), dpi=110)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(ltm.INK)
    ax.scatter(x[keep], 1 - y[keep], s=size, c=colors, linewidths=0)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=ltm.INK)
    plt.close(fig)
    return buf.getvalue()


def sector_pie(items: list[GCC]) -> Optional[bytes]:
    counts = Counter(i.sector or "Other" for i in items)
    if not counts:
        return None
    pairs = counts.most_common()
    fig, ax = plt.subplots(figsize=(4.6, 4.9), dpi=140)
    wedges, _ = ax.pie(
        [v for _, v in pairs], colors=CHART_COLORS[: len(pairs)], startangle=90, counterclock=False,
        wedgeprops={"linewidth": 2, "edgecolor": "white", "width": 0.42},
    )
    ax.text(0, 0.06, str(sum(counts.values())), ha="center", va="center", fontsize=26, fontweight="bold",
            color=ltm.INK)
    ax.text(0, -0.2, "GCCs", ha="center", va="center", fontsize=11, color=ltm.GREY_600)
    ax.legend(wedges, [f"{k} ({v})" for k, v in pairs], loc="upper center", bbox_to_anchor=(0.5, -0.02),
              ncol=2, frameon=False, fontsize=11)
    ax.axis("equal")
    return _png(fig, "white")


def city_bars(items: list[GCC]) -> Optional[bytes]:
    counts = Counter(i.city.split(",")[0].strip() or "Unknown" for i in items)
    if not counts:
        return None
    pairs = counts.most_common(8)[::-1]
    fig, ax = plt.subplots(figsize=(4.6, 0.55 * len(pairs) + 0.6), dpi=140)
    bars = ax.barh([k for k, _ in pairs], [v for _, v in pairs], color=ltm.CORAL, height=0.55)
    for b, (_, v) in zip(bars, pairs):
        ax.text(b.get_width() + 0.05, b.get_y() + b.get_height() / 2, str(v), va="center", fontsize=11,
                color=ltm.INK, fontweight="bold")
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(ltm.GREY_300)
    ax.set_xticks([])
    ax.tick_params(axis="y", labelsize=11, colors=ltm.GREY_900, length=0)
    ax.set_xlim(0, max(v for _, v in pairs) + 0.8)
    return _png(fig, "white")
