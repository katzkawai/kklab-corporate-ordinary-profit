"use strict";
const rows = JSON.parse(document.getElementById("chart-data").textContent);
const sectorLabels = {104: "全産業", 108: "製造業", 144: "非製造業"};
const sizeLabels = {all: "全規模", large: "10億円以上", medium: "1億円以上10億円未満", small: "1億円未満"};
const years = [...new Set(rows.map(row => row.fiscal_year))].sort((a,b) => a-b);

function renderTable(kind, selected) {
  const labels = kind === "industry" ? sectorLabels : Object.fromEntries(Object.entries(sizeLabels).filter(([key]) => key !== "all"));
  const table = document.createElement("table");
  const caption = table.createCaption();
  caption.textContent = `売上高経常利益率（%） / ${kind === "industry" ? sizeLabels[selected] : sectorLabels[selected]}`;
  const head = table.createTHead().insertRow();
  for (const label of ["年度", ...Object.values(labels)]) {
    const th = document.createElement("th"); th.scope = "col"; th.textContent = label; head.append(th);
  }
  const body = table.createTBody();
  for (const year of years) {
    const tr = body.insertRow();
    const th = document.createElement("th"); th.scope = "row"; th.textContent = `${year}年度`; tr.append(th);
    for (const key of Object.keys(labels)) {
      const point = rows.find(row => row.fiscal_year === year && (kind === "industry" ? row.capital_size === selected && row.sector_code === key : row.sector_code === selected && row.capital_size === key));
      tr.insertCell().textContent = point ? point.ordinary_margin_pct.toFixed(2) : "—";
    }
  }
  document.getElementById(`${kind}-table`).replaceChildren(table);
}

for (const kind of ["industry", "size"]) {
  const select = document.getElementById(`${kind}-select`);
  const update = (announce) => {
    const selected = select.value;
    const label = kind === "industry" ? sizeLabels[selected] : sectorLabels[selected];
    const image = document.getElementById(`${kind}-chart`);
    image.src = `assets/${kind}-${selected}.svg`;
    document.getElementById(`${kind}-mobile-source`).srcset = `assets/${kind}-${selected}-mobile.svg`;
    image.alt = `${years[0]}〜${years.at(-1)}年度の売上高経常利益率。${kind === "industry" ? `資本金${label}の業種別比較` : `${label}の資本金規模別比較`}。数値は直後のデータ表で確認できます。`;
    document.getElementById(`${kind}-download`).href = `assets/${kind}-${selected}.png`;
    renderTable(kind, selected);
    if (announce) document.getElementById("chart-status").textContent = `${kind === "industry" ? "業種別" : "規模別"}グラフを${label}に切り替えました。`;
  };
  select.addEventListener("change", () => update(true));
  update(false);
}
