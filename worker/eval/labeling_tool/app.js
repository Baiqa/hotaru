let sample = null;
let articlesById = new Map();
let clusters = {}; // key -> { articleIds: [...] }
let nextNewId = 1;
let saveTimer = null;

async function init() {
  const [sampleRes, labelsRes] = await Promise.all([
    fetch("/api/sample"),
    fetch("/api/labels"),
  ]);
  sample = await sampleRes.json();
  if (sample.error) {
    document.getElementById("empty").textContent = sample.error;
    return;
  }
  for (const a of sample.articles) articlesById.set(a.article_id, a);

  const labels = await labelsRes.json();
  if (labels && labels.clusters && Object.keys(labels.clusters).length > 0) {
    clusters = labels.clusters;
  } else {
    clusters = {};
    for (const [eventId, group] of Object.entries(sample.groups)) {
      clusters[eventId] = { articleIds: [...group.article_ids] };
    }
  }
  render();
}

function isKnownLarge(articleId) {
  const a = articlesById.get(articleId);
  if (!a) return false;
  const g = sample.groups[a.predicted_event_id];
  return g ? g.is_known_large : false;
}

function fmtDate(iso) {
  if (!iso) return "без даты";
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

function truncate(text, n) {
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "…" : text;
}

function clusterKeys() {
  return Object.keys(clusters).filter((k) => clusters[k].articleIds.length > 0);
}

function render() {
  const main = document.getElementById("main");
  main.innerHTML = "";
  const keys = clusterKeys();

  let totalArticles = 0;
  for (const k of keys) totalArticles += clusters[k].articleIds.length;
  document.getElementById("progress").textContent =
    `Групп: ${keys.length} · Статей: ${totalArticles}`;

  if (keys.length === 0) {
    main.innerHTML = '<div id="empty">Нет данных</div>';
    return;
  }

  for (const key of keys) {
    const group = clusters[key];
    const hasKnownLarge = group.articleIds.some(isKnownLarge);

    const groupEl = document.createElement("div");
    groupEl.className = "group" + (hasKnownLarge ? " known-large" : "");

    const header = document.createElement("div");
    header.className = "group-header";
    header.innerHTML = `
      <span class="meta">Группа ${key} · ${group.articleIds.length} статей</span>
      ${hasKnownLarge ? '<span class="badge">⚠ известная проблема</span>' : ""}
      <span class="spacer"></span>
      <label>Объединить с:
        <select data-merge-from="${key}">
          <option value="">—</option>
          ${keys
            .filter((k) => k !== key)
            .map((k) => `<option value="${k}">${k}</option>`)
            .join("")}
        </select>
      </label>
      <button data-merge-btn="${key}">Объединить</button>
    `;
    groupEl.appendChild(header);

    for (const articleId of group.articleIds) {
      const a = articlesById.get(articleId);
      if (!a) continue;
      const row = document.createElement("div");
      row.className = "article";
      row.innerHTML = `
        <div class="article-body">
          <div class="article-title">${a.title ?? "(без заголовка)"}</div>
          <div class="article-meta">${a.source_name} · ${fmtDate(a.published_at)}</div>
          <div class="article-lead">${truncate(a.lead, 160)}</div>
        </div>
        <div class="article-move">
          <select data-move-article="${articleId}" data-move-from="${key}">
            <option value="">переместить в…</option>
            <option value="__new__">+ новая группа</option>
            ${keys
              .filter((k) => k !== key)
              .map((k) => `<option value="${k}">${k}</option>`)
              .join("")}
          </select>
        </div>
      `;
      groupEl.appendChild(row);
    }

    main.appendChild(groupEl);
  }

  main.querySelectorAll("[data-merge-btn]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const fromKey = btn.dataset.mergeBtn;
      const select = main.querySelector(`[data-merge-from="${fromKey}"]`);
      const toKey = select.value;
      if (!toKey) return;
      mergeGroups(fromKey, toKey);
    });
  });

  main.querySelectorAll("[data-move-article]").forEach((select) => {
    select.addEventListener("change", () => {
      const articleId = select.dataset.moveArticle;
      const fromKey = select.dataset.moveFrom;
      const toKey = select.value;
      if (!toKey) return;
      moveArticle(articleId, fromKey, toKey);
    });
  });
}

function moveArticle(articleId, fromKey, toKey) {
  const from = clusters[fromKey];
  from.articleIds = from.articleIds.filter((id) => id !== articleId);
  if (from.articleIds.length === 0) delete clusters[fromKey];

  if (toKey === "__new__") {
    let key;
    do {
      key = `new-${nextNewId++}`;
    } while (clusters[key]);
    clusters[key] = { articleIds: [articleId] };
  } else {
    clusters[toKey].articleIds.push(articleId);
  }
  render();
  save();
}

function mergeGroups(fromKey, toKey) {
  clusters[toKey].articleIds.push(...clusters[fromKey].articleIds);
  delete clusters[fromKey];
  render();
  save();
}

function save() {
  const status = document.getElementById("status");
  status.textContent = "Сохранение…";
  fetch("/api/labels", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clusters }),
  })
    .then(() => {
      status.textContent = "Сохранено " + new Date().toLocaleTimeString("ru-RU");
    })
    .catch(() => {
      status.textContent = "Ошибка сохранения!";
    });
}

init();
