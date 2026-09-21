# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pandas==2.3.3",
#   "matplotlib==3.10.8",
#   "requests==2.32.5",
#   "jinja2==3.1.6",
# ]
# ///
"""法人企業統計の公開CSVから図とGitHub Pagesを生成する。

uv run build.py            # 保存済みの原データから再現（データ取得不要）
uv run build.py --refresh  # e-Stat公開CSVを再取得し、最新の10年度に更新

e-Statウェブサイトの公開CSVダウンロード機能を利用。APIキー不要。
非公開の個票は扱わず、母集団推計値から「合計利益÷合計売上」を計算。
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".cache" / "matplotlib"))
import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager, pyplot as plt
from matplotlib.ticker import MultipleLocator
import pandas as pd
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SID = "0003060791"
BASE = "https://www.e-stat.go.jp/dbview/"
SOURCE = f"https://www.e-stat.go.jp/dbview?sid={SID}"
RAW = ROOT / "data" / "raw" / "estat_annual.csv"
SITE = ROOT / "docs"
SECTORS = {"104": "全産業", "108": "製造業", "144": "非製造業"}
SIZES = {"all": "全規模", "large": "10億円以上", "medium": "1億円以上10億円未満", "small": "1億円未満"}
SIZE_CODES = {"all": ["26"], "large": ["25"], "medium": ["24"], "small": ["19", "16"]}
MEASURES = {"045": "sales_million_yen", "048": "operating_profit_million_yen", "051": "ordinary_profit_million_yen"}
SECTOR_STYLES = {"104": ("#33465b", "o", "--"), "108": ("#007c83", "o", "-"), "144": ("#bf7718", "s", "-")}
SIZE_STYLES = {"large": ("#007c83", "o", "-"), "medium": ("#5864a2", "D", "--"), "small": ("#bf7718", "s", "-")}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def download_csv() -> bytes:
    """ブラウザの公開CSV出力と同じ項目選択・作成・ダウンロードを実行。"""
    session = requests.Session()
    session.headers["User-Agent"] = "kklab-corporate-ordinary-profit/1.0 (public-statistics)"
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1, status_forcelist=[429, 502, 503, 504])))

    def post(endpoint: str, data: dict | None = None) -> requests.Response:
        response = session.post(BASE + endpoint, params={"sid": SID}, data=data or {}, timeout=(20, 180))
        response.raise_for_status()
        return response

    model = post("api_get_model").json()
    require("matters" in model, "e-Statの項目定義を取得できませんでした。")
    time_items = model["matters"]["matter2"]["listData"].values()
    years = sorted(int(v["code"][:4]) for v in time_items if str(v["code"]).endswith("0") and len(v["code"]) == 5)
    latest = max(years)
    selection = {3: ["045", "048", "051", "127"], 4: list(SECTORS), 5: ["26", "25", "24", "19", "16"], 2: [f"{y}0" for y in range(latest - 9, latest + 1)]}
    payload = dict(rows=[], cols=[], tops=[], topsAll=[], viewTops=[], annotationFlg=1,
                   rowNoDataDispFlg=0, colNoDataDispFlg=0, commaType=0, replaceSpChars=0,
                   currentRows=[], currentCols=[], downloadRange=0, fileFormat=2,
                   titleDispFlg=1, codeDispFlg=1, legendDispFlg=1, levelCodeDispFlg=0,
                   auxiliaryCodeDispFlg=0, startNumber=1, totalCellSelected=1,
                   totalCellSelectedCount=600, totalCellCount=600, totalColSelected=10,
                   title="法人企業統計調査", statCode="00350600")
    for mid in [3, 4, 5, 2]:
        matter = model["matters"][f"matter{mid}"]
        item = {key: matter[key] for key in ["matterId", "tableName", "dispTableName", "matterName", "initDisp"]}
        item.update(positionNum=1 if mid == 2 else [3, 4, 5].index(mid) + 1,
                    allSelected=0, allDataSelected=0, allListData=[])
        item["listData"] = [dict(name=v["name"], code=v["code"], unit=v["unitName"])
                            for v in matter["listData"].values() if v["code"] in selection[mid]]
        require(len(item["listData"]) == len(selection[mid]), f"取得項目が不足しています: {mid}")
        payload["cols" if mid == 2 else "rows"].append(item)
    for key in ["rows", "cols", "tops", "topsAll", "viewTops"]:
        payload[key] = base64.b64encode(gzip.compress(json.dumps(payload[key], ensure_ascii=False).encode())).decode()
    print(f"e-Stat公開CSVを取得中: {latest - 9}–{latest}年度", flush=True)
    result = post("api_download_create", payload).json()
    require(isinstance(result, list) and len(result) == 1 and "fileList" in result[0], "CSV作成に失敗しました。e-Statの仕様変更を確認してください。")
    meta = result[0]
    require(len(meta["fileList"]) == 1 and int(meta.get("remainNumber", 0)) == 0, "CSVが分割されました。全件取得を確認してください。")
    file = meta["fileList"][0]
    return post("api_download_run", dict(index=file["fileNo"], path1=meta["filePath"], name1=file["fileName"])).content


def parse_csv(raw: bytes) -> tuple[pd.DataFrame, dict]:
    text = raw.decode("cp932")
    lines = list(csv.reader(io.StringIO(text)))
    header_index = next(i for i, row in enumerate(lines) if row and row[0] == "cat01_code")
    metadata = {row[0]: row[1:] for row in lines[:header_index] if len(row) > 1}
    require(metadata.get("STATUS") == ["0"], "e-Statがエラーを返しています。")
    require(metadata.get("TABLE_INF") == [SID], "想定と異なる統計表です。")
    require(metadata.get("CYCLE") == ["年度次"], "年次別調査のデータではありません。")
    frame = pd.DataFrame(lines[header_index + 1:], columns=lines[header_index])
    require(len(frame) == int(metadata["TOTAL_NUMBER"][0]) == 600, "データ件数が600件と一致しません。")
    keys = ["cat01_code", "cat02_code", "cat03_code", "time_code"]
    require(not frame.duplicated(keys).any(), "項目・業種・規模・年度の重複があります。")
    for col, expected in [("cat01_code", {"045", "048", "051", "127"}), ("cat02_code", set(SECTORS)), ("cat03_code", {"26", "25", "24", "19", "16"})]:
        require(set(frame[col]) == expected, f"想定外の分類: {col}")
    require(frame["annotation"].eq("").all(), "値に注記があります。原表の確認が必要です。")
    frame["value"] = pd.to_numeric(frame["value"], errors="raise")
    require(frame["value"].notna().all(), "欠測値を含みます。0への置換は行いません。")
    require(frame.loc[frame.cat01_code != "127", "unit"].eq("百万円").all(), "金額の単位が異なります。")
    require(frame.loc[frame.cat01_code == "127", "unit"].eq("％").all(), "利益率の単位が異なります。")
    require(frame.time_code.str.fullmatch(r"\d{4}0").all(), "年度コードが不正です。")
    frame["fiscal_year"] = frame.time_code.str[:4].astype(int)
    years = sorted(frame.fiscal_year.unique())
    require(years == list(range(years[-1] - 9, years[-1] + 1)), "10年度分が連続していません。")
    frame = frame.rename(columns={"cat02_code": "sector_code", "cat03_code": "capital_code"})
    wide = frame.pivot(index=["fiscal_year", "sector_code", "capital_code"], columns="cat01_code", values="value").rename(columns=MEASURES).reset_index()
    require(wide.notna().all().all(), "分類の組合せが不足しています。")
    require((wide.sales_million_yen > 0).all(), "売上高が0以下です。")
    calculated = wide.ordinary_profit_million_yen / wide.sales_million_yen * 100
    error = (calculated - wide["127"]).abs().max()
    require(error <= 0.050001, f"公表利益率との照合不一致: 最大差={error}")
    # 金額の丸めに伴う誤差（百万円単位）を許容する。
    residuals = []
    for value in MEASURES.values():
        sector = wide.pivot(index=["fiscal_year", "capital_code"], columns="sector_code", values=value)
        residuals.append(float((sector["104"] - sector["108"] - sector["144"]).abs().max()))
        capital = wide.pivot(index=["fiscal_year", "sector_code"], columns="capital_code", values=value)
        residuals.append(float((capital["26"] - capital[["25", "24", "19", "16"]].sum(axis=1)).abs().max()))
    require(max(residuals) <= 4, f"業種・規模の金額合計が一致しません: {residuals}")
    info = dict(source_url=SOURCE, stats_data_id=SID, source_title="法人企業統計調査・年次別調査／金融業、保険業以外の業種（原数値）",
                published_at=metadata["OPEN_DATE"][0], source_updated_at=metadata["UPDATED_DATE"][0],
                retrieved_at=metadata["DATE"][0], fiscal_year_start=int(years[0]), fiscal_year_end=int(years[-1]),
                raw_sha256=hashlib.sha256(raw).hexdigest(), raw_rows=len(frame),
                published_ratio_max_difference_pp=float(error), aggregate_max_residual_million_yen=max(residuals))
    return wide, info


def aggregate(wide: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for size, codes in SIZE_CODES.items():
        # 比率は平均せず、元の金額を合算して再計算する。
        part = wide[wide.capital_code.isin(codes)].groupby(["fiscal_year", "sector_code"], as_index=False)[list(MEASURES.values())].sum()
        part["capital_size"] = size
        part["capital_label"] = SIZES[size]
        part["sector"] = part.sector_code.map(SECTORS)
        part["ordinary_margin_pct"] = part.ordinary_profit_million_yen / part.sales_million_yen * 100
        part["operating_margin_pct"] = part.operating_profit_million_yen / part.sales_million_yen * 100
        part["net_nonoperating_margin_pp"] = part.ordinary_margin_pct - part.operating_margin_pct
        frames.append(part)
    result = pd.concat(frames, ignore_index=True).sort_values(["fiscal_year", "sector_code", "capital_size"])
    require(len(result) == 120 and result.notna().all().all(), "集計結果が不完全です。")
    return result


def configure_font() -> None:
    candidates = ["Noto Sans CJK JP", "IPAexGothic", "IPAGothic", "Yu Gothic", "Hiragino Sans", "Meiryo"]
    installed = {f.name for f in font_manager.fontManager.ttflist}
    font = next((f for f in candidates if f in installed), None)
    if not font:
        raise RuntimeError("日本語フォントが必要です。Ubuntu: sudo apt-get install fonts-noto-cjk")
    plt.rcParams.update({"font.family": font, "font.size": 11, "axes.unicode_minus": False,
                         "axes.edgecolor": "#ccd5dc", "text.color": "#25374b", "axes.labelcolor": "#526173",
                         "xtick.color": "#526173", "ytick.color": "#526173", "svg.hashsalt": "kklab-profit",
                         "savefig.facecolor": "white"})


def draw_chart(ax, frame: pd.DataFrame, kind: str, selected: str, ymax: float, compact: bool = False) -> None:
    start, end = int(frame.fiscal_year.min()), int(frame.fiscal_year.max())
    if kind == "industry":
        series = [(SECTORS[k], frame[(frame.sector_code == k) & (frame.capital_size == selected)], style) for k, style in SECTOR_STYLES.items()]
    else:
        series = [(SIZES[k], frame[(frame.capital_size == k) & (frame.sector_code == selected)], style) for k, style in SIZE_STYLES.items()]
    # 最新年度の変更点を可視化する。2025年度以降にのみ適用。
    if start <= 2025 <= end:
        ax.axvspan(2024.8, 2025.2, color="#e9edf0", zorder=0)
    label_positions = []
    for name, data, (color, marker, linestyle) in series:
        ax.plot(data.fiscal_year, data.ordinary_margin_pct, label=name, color=color, marker=marker,
                linestyle=linestyle, linewidth=2.5, markersize=5, markeredgecolor="white", markeredgewidth=.7)
        value = data.ordinary_margin_pct.iloc[-1]
        label_positions.append([value, value, color])
    # 近い終点ラベルが重ならないように間隔を確保し、引出線で実際の値を示す。
    label_positions.sort(key=lambda item: item[0])
    for i in range(1, len(label_positions)):
        label_positions[i][1] = max(label_positions[i][1], label_positions[i - 1][1] + ymax * .055)
    for value, label_y, color in label_positions:
        ax.annotate(f"{value:.2f}%", xy=(end, value), xytext=(end + .22, label_y), color=color,
                    va="center", fontweight="bold", fontsize=9 if compact else 11,
                    arrowprops=dict(arrowstyle="-", color=color, linewidth=.8))
    ax.set(xlim=(start - .25, end + 1.05), ylim=(0, ymax), xlabel="年度", ylabel="売上高経常利益率（%）")
    ax.set_xticks([start, start + 2, start + 4, start + 6, end] if compact else list(range(start, end + 1)))
    ax.yaxis.set_major_locator(MultipleLocator(2))
    ax.grid(axis="y", color="#e7ecf0", linewidth=.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="both", length=0, pad=9)
    if compact:
        ax.tick_params(axis="both", labelsize=10, pad=7)
        ax.legend(loc="upper left", bbox_to_anchor=(-.03, 1.38), ncol=1, frameon=False, fontsize=10)
    else:
        ax.legend(loc="upper left", bbox_to_anchor=(0, 1.15), ncol=3, frameon=False, fontsize=10, columnspacing=2)


def render_charts(frame: pd.DataFrame) -> list[dict]:
    configure_font()
    out = SITE / "assets"
    out.mkdir(parents=True, exist_ok=True)
    ymax = max(10, math.ceil((frame.ordinary_margin_pct.max() + 1) / 2) * 2)
    charts = []
    for kind, choices in [("industry", SIZES), ("size", SECTORS)]:
        for selected, label in choices.items():
            fig, ax = plt.subplots(figsize=(12, 5.4))
            fig.subplots_adjust(left=.075, right=.97, top=.79, bottom=.15)
            title = f"業種別の推移｜資本金：{label}" if kind == "industry" else f"資本金規模別の推移｜{label}"
            fig.text(.075, .965, title, fontsize=15, fontweight="bold", va="top")
            draw_chart(ax, frame, kind, selected, ymax)
            fig.text(.075, .02, "出典：財務省「法人企業統計調査」年次別調査（e-Stat）｜金融業・保険業を除く", color="#64748b", fontsize=9)
            name = f"{kind}-{selected}"
            for extension in ["svg", "png"]:
                fig.savefig(out / f"{name}.{extension}", dpi=180, metadata={"Date": None} if extension == "svg" else None)
            plt.close(fig)
            fig, ax = plt.subplots(figsize=(5.6, 4.8))
            fig.subplots_adjust(left=.13, right=.95, top=.67, bottom=.16)
            draw_chart(ax, frame, kind, selected, ymax, compact=True)
            fig.text(.05, .96, title, fontsize=12, fontweight="bold", va="top")
            fig.savefig(out / f"{name}-mobile.svg", metadata={"Date": None})
            plt.close(fig)
            charts.append(dict(id=name, title=title))
    fig, axes = plt.subplots(2, 1, figsize=(12, 11))
    fig.subplots_adjust(left=.085, right=.97, top=.83, bottom=.10, hspace=.65)
    fig.suptitle("日本企業の売上高経常利益率", x=.085, ha="left", fontsize=20, fontweight="bold")
    for ax, kind, selected, title in zip(axes, ["industry", "size"], ["all", "104"], ["業種別（全規模）", "資本金規模別（全産業）"]):
        ax.set_title(title, loc="left", pad=48, fontsize=13)
        draw_chart(ax, frame, kind, selected, ymax)
    fig.text(.085, .018, "出典：財務省「法人企業統計調査」年次別調査（e-Stat）。金融業・保険業を除く。\n2025年度から資本金5億円以上の未回答法人の補完方法が変更。比率は合計経常利益÷合計売上高×100。", fontsize=9, color="#64748b")
    fig.savefig(out / "ordinary-profit-margin.png", dpi=180)
    fig.savefig(out / "ordinary-profit-margin.svg", metadata={"Date": None})
    plt.close(fig)
    return charts


def make_context(frame: pd.DataFrame, info: dict) -> dict:
    start, end = info["fiscal_year_start"], info["fiscal_year_end"]

    def row(year, sector="104", size="all"):
        return frame[(frame.fiscal_year == year) & (frame.sector_code == sector) & (frame.capital_size == size)].iloc[0]

    latest, first = row(end), row(start)
    m, n = row(end, "108"), row(end, "144")
    large, medium, small = (row(end, size=s) for s in ["large", "medium", "small"])
    gap_now = large.ordinary_margin_pct - small.ordinary_margin_pct
    gap_then = row(start, size="large").ordinary_margin_pct - row(start, size="small").ordinary_margin_pct
    hist = frame[(frame.sector_code == "104") & (frame.capital_size == "all")]
    low = hist.loc[hist.ordinary_margin_pct.idxmin()]
    insights = [
        dict(title="全産業の利益率の長期的な変化", body=f"全産業の利益率は{start}年度の{first.ordinary_margin_pct:.2f}%から{end}年度の{latest.ordinary_margin_pct:.2f}%へ、{latest.ordinary_margin_pct - first.ordinary_margin_pct:.2f}ポイント変化。売上高に対する経常利益の厚みを比較できる。", tag="全体の推移"),
        dict(title="業種間の差は、本業と営業外収支の両方から", body=f"{end}年度は製造業{m.ordinary_margin_pct:.2f}%、非製造業{n.ordinary_margin_pct:.2f}%。営業利益率はそれぞれ{m.operating_margin_pct:.2f}%・{n.operating_margin_pct:.2f}%で、経常利益率との差（営業外収支／売上高）は{m.net_nonoperating_margin_pp:.2f}・{n.net_nonoperating_margin_pp:.2f}ポイント。経常利益率だけで本業の稼ぐ力を判断しない。", tag="収益の構造"),
        dict(title="資本金規模の間に持続する利益率の差", body=f"{end}年度は10億円以上が{large.ordinary_margin_pct:.2f}%、1億円未満が{small.ordinary_margin_pct:.2f}%。両者の差は{start}年度の{gap_then:.2f}ポイントから{gap_now:.2f}ポイントへ。業種構成や営業外収支の違いも含むため、規模そのものの因果効果とはいえない。", tag="規模間の格差"),
        dict(title=f"{int(low.fiscal_year)}年度の低水準から回復", body=f"対象期間で全産業が最も低いのは{int(low.fiscal_year)}年度の{low.ordinary_margin_pct:.2f}%。{end}年度はそこから{latest.ordinary_margin_pct - low.ordinary_margin_pct:.2f}ポイント高い。ただし、各年度の集計は同じ企業を追跡した結果ではなく、母集団や業種構成の変化も反映する。", tag="変動の読み方"),
    ]
    return dict(info=info, start=start, end=end, latest=latest, first=first, manufacturing=m, nonmanufacturing=n,
                large=large, medium=medium, small=small, gap=gap_now, sectors=SECTORS, sizes=SIZES,
                insights=insights, rows=frame.to_dict(orient="records"),
                years=list(range(start, end + 1)), source_url=SOURCE)


def build(refresh: bool = False) -> None:
    raw = download_csv() if refresh else RAW.read_bytes()
    wide, info = parse_csv(raw)
    frame = aggregate(wide)
    if refresh:
        # 検証が完了した原データのみ保存済みスナップショットを置換。
        RAW.parent.mkdir(parents=True, exist_ok=True)
        RAW.write_bytes(raw)
    charts = render_charts(frame)
    for directory in [ROOT / "data", SITE / "data"]:
        directory.mkdir(parents=True, exist_ok=True)
        frame.to_csv(directory / "ordinary_profit_margin.csv", index=False, encoding="utf-8-sig", float_format="%.8f")
        (directory / "provenance.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(RAW, SITE / "data" / "estat_annual.csv")
    context = make_context(frame, info)
    env = Environment(loader=FileSystemLoader(ROOT / "web"), autoescape=select_autoescape(["html"]))
    (SITE / "index.html").write_text(env.get_template("index.html").render(**context), encoding="utf-8")
    for asset in ["style.css", "app.js"]:
        shutil.copy2(ROOT / "web" / asset, SITE / "assets" / asset)
    shutil.copy2(ROOT / "build.py", SITE / "build.py")
    (SITE / ".nojekyll").touch()
    print(json.dumps(dict(**info, processed_rows=len(frame), charts=len(charts)), ensure_ascii=False, indent=2))
    print(frame[(frame.fiscal_year == info["fiscal_year_end"]) & ((frame.capital_size == "all") | (frame.sector_code == "104"))][["fiscal_year", "sector", "capital_label", "ordinary_margin_pct", "operating_margin_pct"]].to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="e-Statから最新の10年度分を再取得（ネットワークが必要）")
    build(parser.parse_args().refresh)
