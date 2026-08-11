const APPLICATION_BASE_URL = new URL("./", document.baseURI);
const CHAT_SESSION_KEY = "reposcout.ask-session.v1";
const MAX_VISIBLE_CHAT_MESSAGES = 24;
const MAX_CHAT_COMPOSER_HEIGHT = 160;
const UNCERTAIN_COMPLETION_MESSAGE =
  "RepoScout couldn't confirm the final response. A requested action may already have completed. Check My Projects before retrying.";
const CANCELLED_COMPLETION_MESSAGE =
  "Stopped waiting. If this request included saving a project, changing its status, or adding a note, that action may already have completed. Check My Projects before retrying.";
const SAFE_CANCELLATION_MESSAGE = "Stopped. No project changes were started.";
const ASSISTANT_PROGRESS_COPY = Object.freeze({
  working: "Working through your request…",
  searching_projects: "Searching projects…",
  reviewing_details: "Reviewing project details…",
  saving_projects: "Saving projects…",
  updating_status: "Updating project status…",
  adding_notes: "Adding notes…",
  continuing: "Continuing your request…",
  finishing: "Finishing up…",
});
const CHAT_ONBOARDING_MESSAGE = `Welcome to RepoScout. I can help you discover open-source GitHub projects and work with the evidence in their indexed README documentation.

- Search for projects using a natural-language goal, then ask for evidence-based details or comparisons.
- Save useful projects, add notes, and organize them as Interested, To Try, In Progress, or Completed.
- Follow up naturally with requests such as “Tell me more about the second one,” “Compare those two,” or “Save it.”

What would you like to find or organize?`;

class ApiError extends Error {
  constructor(status, detail, retryAfter = null, uncertain = false) {
    super("RepoScout request failed");
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
    this.uncertain = uncertain;
  }
}

function apiUrl(relativePath) {
  if (
    typeof relativePath !== "string" ||
    relativePath.length === 0 ||
    relativePath.startsWith("/") ||
    relativePath.startsWith("//") ||
    /^[a-z][a-z\d+.-]*:/i.test(relativePath)
  ) {
    throw new TypeError("API paths must be relative to the application base");
  }
  return new URL(relativePath, APPLICATION_BASE_URL);
}

function apiFetch(relativePath, options = {}) {
  return fetch(apiUrl(relativePath), options);
}

async function apiRequest(relativePath, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  const response = await apiFetch(relativePath, { ...options, headers });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    if (!response.ok) {
      throw new ApiError(response.status, null, response.headers.get("Retry-After"));
    }
  }

  if (!response.ok) {
    throw new ApiError(
      response.status,
      payload?.detail ?? null,
      response.headers.get("Retry-After"),
      response.headers.get("X-RepoScout-Completion") === "uncertain",
    );
  }
  return payload;
}

function parseSseEvent(frame) {
  let eventName = "message";
  const dataLines = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(":")) {
      continue;
    }
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  try {
    return { event: eventName, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    throw new ApiError(502, "RepoScout returned an invalid progress response", null, true);
  }
}

async function assistantStreamRequest(payload, { signal, onProgress }) {
  const response = await apiFetch("assistant/messages/stream", {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    let detail = null;
    try {
      detail = (await response.json())?.detail ?? null;
    } catch {
      // The status remains sufficient for safe public error mapping.
    }
    throw new ApiError(
      response.status,
      detail,
      response.headers.get("Retry-After"),
      response.headers.get("X-RepoScout-Completion") === "uncertain",
    );
  }
  if (!response.body) {
    throw new ApiError(502, "RepoScout progress is unavailable", null, true);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      let boundary = buffer.match(/\r?\n\r?\n/);
      while (boundary && boundary.index !== undefined) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const parsed = parseSseEvent(frame);
        if (parsed?.event === "progress") {
          if (typeof parsed.data?.phase !== "string" || !(parsed.data.phase in ASSISTANT_PROGRESS_COPY)) {
            throw new ApiError(502, "RepoScout returned invalid progress", null, true);
          }
          onProgress(parsed.data.phase);
        } else if (parsed?.event === "result") {
          return parsed.data;
        } else if (parsed?.event === "error") {
          throw new ApiError(
            Number(parsed.data?.status) || 502,
            typeof parsed.data?.detail === "string" ? parsed.data.detail : null,
            parsed.data?.retry_after ? String(parsed.data.retry_after) : null,
            parsed.data?.uncertain === true,
          );
        } else if (parsed !== null) {
          throw new ApiError(502, "RepoScout returned an unknown progress event", null, true);
        }
        boundary = buffer.match(/\r?\n\r?\n/);
      }
      if (done) {
        break;
      }
    }
  } finally {
    reader.releaseLock();
  }
  throw new ApiError(502, "RepoScout could not confirm the final response", null, true);
}

function createTurnId() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function element(tagName, className = "", text = "") {
  const node = document.createElement(tagName);
  if (className) {
    node.className = className;
  }
  if (text) {
    node.textContent = text;
  }
  return node;
}

function formatCount(value) {
  const count = Number(value);
  return Number.isFinite(count) && count >= 0 ? new Intl.NumberFormat().format(count) : "—";
}

function formatIndexedTime(value) {
  if (!value) {
    return "Not indexed yet";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Index time unavailable";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function setMetricValue(node, value) {
  node.textContent = formatCount(value);
  node.classList.remove("metric-loading");
}

function joinReasonPhrases(phrases) {
  if (phrases.length === 0) {
    return "";
  }
  if (phrases.length === 1) {
    return phrases[0];
  }
  if (phrases.length === 2) {
    return `${phrases[0]}, and ${phrases[1]}`;
  }
  return `${phrases.slice(0, -1).join(", ")}, and ${phrases.at(-1)}`;
}

function corpusReadinessCopy(summary) {
  const searchable = formatCount(summary.repositories_searchable);
  const ingested = formatCount(summary.repositories_ingested);
  const reasons = summary.not_searchable_reasons;
  const phrases = [];

  if (reasons.awaiting_indexing > 0) {
    const verb = reasons.awaiting_indexing === 1 ? "is" : "are";
    phrases.push(`${formatCount(reasons.awaiting_indexing)} ${verb} awaiting indexing`);
  }
  if (reasons.missing_readme > 0) {
    const verb = reasons.missing_readme === 1 ? "doesn't" : "don't";
    phrases.push(
      `${formatCount(reasons.missing_readme)} ${verb} have README content available`,
    );
  }
  if (reasons.retrieval_error > 0) {
    phrases.push(`${formatCount(reasons.retrieval_error)} could not currently be retrieved`);
  }
  if (reasons.other > 0) {
    const verb = reasons.other === 1 ? "is" : "are";
    phrases.push(`${formatCount(reasons.other)} ${verb} currently unavailable`);
  }

  let copy = `${searchable} of ${ingested} repositories are searchable.`;
  const explanation = joinReasonPhrases(phrases);
  if (explanation) {
    copy += ` ${explanation}.`;
  }
  return `${copy} Last indexed ${formatIndexedTime(summary.last_indexed_at)}`;
}

async function loadCorpusSummary() {
  const metrics = document.querySelector("#corpus-metrics");
  const detail = document.querySelector("#corpus-detail");
  const retry = document.querySelector("#corpus-retry");
  metrics.setAttribute("aria-busy", "true");
  retry.hidden = true;

  try {
    const summary = await apiRequest("corpus/summary");
    setMetricValue(document.querySelector("#metric-repositories"), summary.repositories_ingested);
    setMetricValue(document.querySelector("#metric-searchable"), summary.repositories_searchable);
    setMetricValue(document.querySelector("#metric-chunks"), summary.searchable_chunks);
    detail.textContent = corpusReadinessCopy(summary);
  } catch {
    detail.textContent = "Search readiness is temporarily unavailable. Search is still available.";
    retry.hidden = false;
  } finally {
    metrics.setAttribute("aria-busy", "false");
  }
}

function activeViewFromHash() {
  if (window.location.hash === "#ask") {
    return "ask";
  }
  return window.location.hash === "#projects" ? "projects" : "discover";
}

function scrollElementIntoView(target) {
  if (!(target instanceof HTMLElement)) {
    return;
  }
  target.scrollIntoView({
    behavior: prefersReducedMotion() ? "auto" : "smooth",
    block: "start",
  });
}

function showView(viewName, { moveFocus = false, scrollToWorkspace = false } = {}) {
  const title = document.querySelector("#mode-title");
  const titles = {
    ask: "Ask RepoScout",
    projects: "My Projects",
    discover: "Discover Projects",
  };
  title.textContent = titles[viewName];

  for (const view of document.querySelectorAll("[data-view]")) {
    const active = view.dataset.view === viewName;
    view.hidden = !active;
    if (active) {
      view.classList.remove("is-entering");
      void view.offsetWidth;
      view.classList.add("is-entering");
    }
  }
  for (const link of document.querySelectorAll("[data-nav-view]")) {
    if (link.dataset.navView === viewName) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  }
  if (moveFocus) {
    title.setAttribute("tabindex", "-1");
    title.focus({ preventScroll: true });
  }
  if (viewName === "projects") {
    loadSavedProjects();
  } else if (viewName === "ask") {
    scrollChatToLatest(chatState.messages.length === 0);
  }
  if (scrollToWorkspace) {
    window.requestAnimationFrame(() => {
      scrollElementIntoView(document.querySelector("#search-workspace"));
    });
  }
}

function setupPrimaryNavigation() {
  for (const link of document.querySelectorAll("[data-nav-view]")) {
    link.addEventListener("click", (event) => {
      if (link.dataset.navView !== activeViewFromHash()) {
        return;
      }
      event.preventDefault();
      showView(link.dataset.navView, { moveFocus: true, scrollToWorkspace: true });
    });
  }

  document.querySelector(".brand").addEventListener("click", (event) => {
    event.preventDefault();
    if (window.location.hash !== "#discover") {
      window.history.pushState(
        null,
        "",
        `${window.location.pathname}${window.location.search}#discover`,
      );
    }
    showView("discover");
    window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? "auto" : "smooth" });
    });
  });
}

function normalizeInitialHash() {
  if (!["#discover", "#ask", "#projects"].includes(window.location.hash)) {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#discover`);
  }
  showView(activeViewFromHash());
}

function setupExampleQueries() {
  for (const button of document.querySelectorAll("[data-query-target]")) {
    const query = document.querySelector(`#${button.dataset.queryTarget}`);
    if (!(query instanceof HTMLTextAreaElement)) {
      continue;
    }
    button.addEventListener("click", () => {
      query.value = button.textContent.trim();
      query.setAttribute("aria-invalid", "false");
      query.focus();
    });
  }
}

function buildSearchPayload(form) {
  const data = new FormData(form);
  const query = String(data.get("query") ?? "").trim();
  const language = String(data.get("language") ?? "").trim();
  const rawStars = String(data.get("minimum_stars") ?? "").trim();
  const rawTopK = String(data.get("top_k") ?? "").trim();
  const filters = {};

  if (language) {
    filters.language = language;
  }
  if (rawStars) {
    filters.minimum_stars = Number(rawStars);
  }

  return {
    query,
    top_k: Number(rawTopK),
    ...(Object.keys(filters).length > 0 ? { filters } : {}),
  };
}

function validateSearchForm(form, payload) {
  const query = form.querySelector("textarea[name='query']");
  const stars = form.querySelector("input[name='minimum_stars']");
  const topK = form.querySelector("input[name='top_k']");
  query.setAttribute("aria-invalid", "false");
  stars.setAttribute("aria-invalid", "false");
  topK.setAttribute("aria-invalid", "false");

  if (!payload.query) {
    query.setAttribute("aria-invalid", "true");
    query.focus();
    return "Describe what you want RepoScout to find.";
  }
  if (payload.query.length > 500) {
    query.setAttribute("aria-invalid", "true");
    query.focus();
    return "Keep your request to 500 characters or fewer.";
  }
  if (
    payload.filters?.minimum_stars !== undefined &&
    (!Number.isInteger(payload.filters.minimum_stars) || payload.filters.minimum_stars < 0)
  ) {
    stars.setAttribute("aria-invalid", "true");
    stars.focus();
    return "Minimum stars must be a nonnegative whole number.";
  }
  if (!Number.isInteger(payload.top_k) || payload.top_k < 1 || payload.top_k > 10) {
    topK.setAttribute("aria-invalid", "true");
    topK.focus();
    return "Number of results must be a whole number from 1 to 10.";
  }
  return null;
}

function setStatus(node, message = "", kind = "") {
  node.textContent = message;
  node.className = "request-status";
  if (kind) {
    node.classList.add(`is-${kind}`);
  }
}

function createLoadingPanel() {
  const panel = element("div", "loading-panel is-entering");
  panel.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 4; index += 1) {
    panel.append(element("span", "skeleton-line"));
  }
  return panel;
}

function showLoading(results, count = 2) {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < count; index += 1) {
    fragment.append(createLoadingPanel());
  }
  results.replaceChildren(fragment);
  results.setAttribute("aria-busy", "true");
}

function addCoverageAction(container, searchQuery) {
  const action = element("button", "coverage-action-button", "Request more coverage");
  action.type = "button";
  action.addEventListener("click", () => openCoveragePanel(searchQuery));
  container.append(action);
}

function showEmpty(results, title, message, coverageQuery = "") {
  const panel = element("div", "empty-panel");
  panel.append(element("h3", "", title), element("p", "", message));
  addCoverageAction(panel, coverageQuery);
  results.replaceChildren(panel);
}

function friendlyError(error) {
  if (!(error instanceof ApiError)) {
    return "RepoScout could not reach the service. Check your connection and try again.";
  }
  if (error.status === 422) {
    return "Check your request and filters, then try again.";
  }
  if (error.status === 502) {
    return "RepoScout could not complete the generated answer. Please try again.";
  }
  if (error.status === 503) {
    const retry = /^\d+$/.test(error.retryAfter ?? "") ? ` Try again in ${error.retryAfter} seconds.` : "";
    return `RepoScout is temporarily unavailable.${retry}`;
  }
  if (error.status === 504) {
    return "The answer took too long to generate. Please try again.";
  }
  return "RepoScout could not complete that request. Please try again.";
}

function safeGitHubUrl(value) {
  if (typeof value !== "string") {
    return null;
  }
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && url.hostname.toLowerCase() === "github.com") {
      return url;
    }
  } catch {
    return null;
  }
  return null;
}

function metadataChip(text) {
  return element("span", "metadata-chip", text);
}

function evidenceDomId(project, chunk) {
  return `evidence-${project.repo_id}-${chunk.chunk_index}`;
}

function readmeMatchStrength(similarity) {
  if (!Number.isFinite(similarity) || similarity < 0.25 || similarity > 1) {
    return null;
  }
  if (similarity >= 0.6) {
    return "Strong";
  }
  if (similarity >= 0.45) {
    return "Moderate";
  }
  return "Limited";
}

function createReadmeMatchIndicator(similarity) {
  const strength = readmeMatchStrength(similarity);
  if (strength === null) {
    return null;
  }
  const indicator = element("div", "readme-match");
  indicator.append(
    element("span", "readme-match-label", `README match: ${strength}`),
    element(
      "span",
      "readme-match-context",
      "Based on semantic similarity between your request and the indexed README.",
    ),
  );
  return indicator;
}

function createProjectCard(
  project,
  citationTargets,
  { showRank = true, showActionSlot = true, evidenceIdPrefix = "" } = {},
) {
  const card = element("article", "project-card is-entering");
  card.dataset.repoId = String(project.repo_id ?? "");

  const header = element("div", "project-header");
  const titleWrap = element("div", "project-title-wrap");
  if (showRank && Number.isInteger(project.rank) && project.rank > 0) {
    titleWrap.append(element("span", "rank-badge", String(project.rank)));
  }
  titleWrap.append(element("h3", "", project.full_name || project.name || "Unnamed repository"));
  header.append(titleWrap);

  const githubUrl = safeGitHubUrl(project.html_url);
  if (githubUrl) {
    const link = element("a", "github-link", "View on GitHub ↗");
    link.href = githubUrl.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    header.append(link);
  }
  card.append(header);

  card.append(
    element(
      "p",
      "project-description",
      project.description || "No project description is currently available.",
    ),
  );

  const metadata = element("div", "metadata-row");
  if (project.primary_language) {
    metadata.append(metadataChip(project.primary_language));
  }
  if (Number.isInteger(project.stars) && project.stars >= 0) {
    metadata.append(metadataChip(`★ ${formatCount(project.stars)} stars`));
  }
  if (Number.isInteger(project.forks) && project.forks >= 0) {
    metadata.append(metadataChip(`${formatCount(project.forks)} forks`));
  }
  if (project.license) {
    metadata.append(metadataChip(project.license));
  }
  if (metadata.childElementCount > 0) {
    card.append(metadata);
  }

  if (Array.isArray(project.topics) && project.topics.length > 0) {
    const topics = element("div", "topic-list");
    for (const topic of project.topics.slice(0, 8)) {
      topics.append(element("span", "topic-chip", topic));
    }
    card.append(topics);
  }

  if (showActionSlot) {
    card.append(element("div", "project-action-slot"));
  }

  if (Array.isArray(project.evidence) && project.evidence.length > 0) {
    const details = element("details", "evidence-group");
    details.append(element("summary", "", "Why this matched"));
    const list = element("div", "evidence-list");
    const matchIndicator = createReadmeMatchIndicator(project.similarity);
    if (matchIndicator) {
      list.append(matchIndicator);
    }

    for (const chunk of project.evidence) {
      const evidence = element("div", "evidence-chunk", chunk.chunk_text || "Evidence unavailable.");
      const baseId = evidenceDomId(project, chunk);
      evidence.id = evidenceIdPrefix ? `${evidenceIdPrefix}-${baseId}` : baseId;
      evidence.setAttribute("tabindex", "-1");
      list.append(evidence);

      const citation = `[${project.full_name}#chunk-${chunk.chunk_index}]`;
      citationTargets.set(citation, { details, evidence });
    }
    details.append(list);
    card.append(details);
  }

  return card;
}

function appendAnswerInline(parent, text, citationTargets) {
  const pattern = /(\[[^\]\n]+\]\(https:\/\/github\.com\/[^)\s]+\)|\[[^\]\n\s]+\/[^\]\n\s]+#chunk-\d+\]|\*\*[^*\n]+\*\*)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) {
      parent.append(document.createTextNode(text.slice(cursor, match.index)));
    }
    const token = match[0];
    const markdownLink = token.match(/^\[([^\]\n]+)\]\((https:\/\/github\.com\/[^)\s]+)\)$/);
    if (markdownLink) {
      const githubUrl = safeGitHubUrl(markdownLink[2]);
      if (githubUrl) {
        const link = element("a", "answer-link", markdownLink[1]);
        link.href = githubUrl.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.append(link);
      } else {
        parent.append(document.createTextNode(token));
      }
    } else if (token.startsWith("**") && token.endsWith("**")) {
      parent.append(element("strong", "", token.slice(2, -2)));
    } else if (citationTargets.has(token)) {
      const button = element("button", "citation-button", token);
      button.type = "button";
      button.setAttribute("aria-label", `Open evidence ${token}`);
      button.addEventListener("click", () => {
        const target = citationTargets.get(token);
        target.details.open = true;
        target.evidence.scrollIntoView({ behavior: "smooth", block: "center" });
        target.evidence.focus({ preventScroll: true });
      });
      parent.append(button);
    } else {
      parent.append(document.createTextNode(token));
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) {
    parent.append(document.createTextNode(text.slice(cursor)));
  }
}

function normalizeAnswerMarkdown(answer) {
  return String(answer ?? "")
    .replace(/^(\s*\d+[.)])(?=\[)/gm, "$1 ")
    .replace(
      /\]\s+\((https:\/\/github\.com\/[^)\s]+)\)/g,
      "]($1)",
    )
    .replace(/\s*\(repo_id\s*:\s*\d+\)/gi, "");
}

function renderAnswerBody(container, answer, citationTargets) {
  const lines = normalizeAnswerMarkdown(answer).split(/\r?\n/);
  let orderedList = null;
  let currentOrderedItem = null;
  let nestedBulletList = null;
  let unorderedList = null;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      continue;
    }

    const bullet = line.match(/^[-*]\s+(.+)/);
    const numbered = line.match(/^(\d+)[.)]\s+(.+)/);
    if (numbered) {
      if (orderedList === null) {
        orderedList = element("ol");
        orderedList.start = Number(numbered[1]);
        container.append(orderedList);
      }
      currentOrderedItem = element("li");
      appendAnswerInline(currentOrderedItem, numbered[2], citationTargets);
      orderedList.append(currentOrderedItem);
      nestedBulletList = null;
      unorderedList = null;
      continue;
    }
    if (bullet) {
      if (currentOrderedItem) {
        if (nestedBulletList === null) {
          nestedBulletList = element("ul");
          currentOrderedItem.append(nestedBulletList);
        }
        const item = element("li");
        appendAnswerInline(item, bullet[1], citationTargets);
        nestedBulletList.append(item);
      } else {
        if (unorderedList === null) {
          unorderedList = element("ul");
          container.append(unorderedList);
        }
        const item = element("li");
        appendAnswerInline(item, bullet[1], citationTargets);
        unorderedList.append(item);
      }
      continue;
    }

    orderedList = null;
    currentOrderedItem = null;
    nestedBulletList = null;
    unorderedList = null;
    const heading = line.match(/^#{1,4}\s+(.+)/);
    const paragraph = element(heading ? "h4" : "p");
    appendAnswerInline(paragraph, heading ? heading[1] : line, citationTargets);
    container.append(paragraph);
  }
}

function renderProjects(
  projects,
  results,
  { emptyTitle, emptyMessage, coverageQuery = "" } = {},
) {
  if (!Array.isArray(projects) || projects.length === 0) {
    showEmpty(results, emptyTitle, emptyMessage, coverageQuery);
    return;
  }
  const targets = new Map();
  const fragment = document.createDocumentFragment();
  for (const project of projects) {
    fragment.append(createProjectCard(project, targets));
  }
  results.replaceChildren(fragment);
}

const chatState = {
  conversationId: null,
  messages: [],
  blocked: false,
};

function validStoredChatMessage(value) {
  const validPresentation =
    value?.presentation === undefined ||
    ["cards", "references", "text"].includes(value.presentation);
  const validEvidence =
    value?.evidence === undefined ||
    (Array.isArray(value.evidence) &&
      value.evidence.length <= 10 &&
      value.evidence.every(validStoredAssistantEvidenceProject));
  return (
    value !== null &&
    typeof value === "object" &&
    ["user", "assistant"].includes(value.role) &&
    typeof value.content === "string" &&
    value.content.trim().length > 0 &&
    value.content.length <= (value.role === "user" ? 2000 : 20000) &&
    (value.role === "assistant" ? validPresentation : value.presentation === undefined) &&
    (value.role === "assistant" ? validEvidence : value.evidence === undefined)
  );
}

function validStoredAssistantEvidenceProject(project) {
  const validOptionalText = (value, maximum) =>
    value === undefined || value === null || (typeof value === "string" && value.length <= maximum);
  const validOptionalCount = (value) =>
    value === undefined || (Number.isInteger(value) && value >= 0);
  const validOptionalSimilarity = (value) =>
    value === undefined || value === null || (Number.isFinite(value) && value >= -1 && value <= 1);
  return (
    project !== null &&
    typeof project === "object" &&
    Number.isInteger(project.repo_id) &&
    project.repo_id > 0 &&
    typeof project.full_name === "string" &&
    project.full_name.length > 0 &&
    project.full_name.length <= 200 &&
    validOptionalText(project.name, 200) &&
    validOptionalText(project.owner, 200) &&
    validOptionalText(project.description, 2000) &&
    validOptionalText(project.primary_language, 100) &&
    validOptionalText(project.license, 100) &&
    validOptionalCount(project.stars) &&
    validOptionalCount(project.forks) &&
    validOptionalCount(project.open_issues) &&
    validOptionalSimilarity(project.similarity) &&
    (project.topics === undefined ||
      (Array.isArray(project.topics) &&
        project.topics.length <= 8 &&
        project.topics.every((topic) => typeof topic === "string" && topic.length <= 100))) &&
    safeGitHubUrl(project.html_url) !== null &&
    Array.isArray(project.evidence) &&
    project.evidence.length <= 5 &&
    project.evidence.every(
      (chunk) =>
        chunk !== null &&
        typeof chunk === "object" &&
        Number.isInteger(chunk.chunk_index) &&
        chunk.chunk_index >= 0 &&
        typeof chunk.chunk_text === "string" &&
        chunk.chunk_text.trim().length > 0 &&
        chunk.chunk_text.length <= 4000 &&
        validOptionalSimilarity(chunk.similarity),
    )
  );
}

function restoreChatSession() {
  try {
    const stored = JSON.parse(sessionStorage.getItem(CHAT_SESSION_KEY) ?? "null");
    if (stored === null || typeof stored !== "object") {
      return;
    }
    if (typeof stored.conversationId !== "string") {
      return;
    }
    if (!Array.isArray(stored.messages) || !stored.messages.every(validStoredChatMessage)) {
      return;
    }
    chatState.conversationId = stored.conversationId;
    chatState.messages = stored.messages.slice(-MAX_VISIBLE_CHAT_MESSAGES);
  } catch {
    sessionStorage.removeItem(CHAT_SESSION_KEY);
  }
}

function persistChatSession() {
  try {
    if (!chatState.conversationId) {
      sessionStorage.removeItem(CHAT_SESSION_KEY);
      return;
    }
    sessionStorage.setItem(
      CHAT_SESSION_KEY,
      JSON.stringify({
        conversationId: chatState.conversationId,
        messages: chatState.messages.slice(-MAX_VISIBLE_CHAT_MESSAGES),
      }),
    );
  } catch {
    // The current page conversation remains usable when tab storage is unavailable.
  }
}

function createChatProjectCards(evidenceProjects, citationTargets, messageIndex) {
  if (!Array.isArray(evidenceProjects) || evidenceProjects.length === 0) {
    return null;
  }
  const cards = element("div", "chat-project-grid");
  for (const project of evidenceProjects) {
    cards.append(
      createProjectCard(project, citationTargets, {
        showRank: false,
        showActionSlot: false,
        evidenceIdPrefix: `chat-${messageIndex}`,
      }),
    );
  }
  return cards;
}

function chatPresentation(message) {
  if (["cards", "references", "text"].includes(message.presentation)) {
    return message.presentation;
  }
  return Array.isArray(message.evidence) && message.evidence.length > 0 ? "cards" : "text";
}

function createChatProjectReferences(evidenceProjects) {
  if (!Array.isArray(evidenceProjects) || evidenceProjects.length === 0) {
    return null;
  }
  const references = element("div", "chat-project-references");
  references.append(element("span", "chat-project-references-label", "Referenced projects"));
  const list = element("ul", "chat-project-reference-list");
  const seenRepoIds = new Set();
  for (const project of evidenceProjects) {
    if (seenRepoIds.has(project.repo_id)) {
      continue;
    }
    const githubUrl = safeGitHubUrl(project.html_url);
    if (!githubUrl) {
      continue;
    }
    seenRepoIds.add(project.repo_id);
    const item = element("li");
    const link = element("a", "chat-project-reference-link", `${project.full_name} ↗`);
    link.href = githubUrl.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    item.append(link);
    list.append(item);
  }
  if (list.childElementCount === 0) {
    return null;
  }
  references.append(list);
  return references;
}

function createChatMessage(message, animate = false, messageIndex = 0) {
  const item = element(
    "article",
    `chat-message is-${message.role}${animate ? " is-entering" : ""}`,
  );
  item.append(element("span", "chat-speaker", message.role === "user" ? "You" : "RepoScout"));
  const body = element("div", "chat-message-body");
  if (message.role === "assistant") {
    const presentation = chatPresentation(message);
    const citationTargets = new Map();
    const projectCards =
      presentation === "cards"
        ? createChatProjectCards(message.evidence, citationTargets, messageIndex)
        : null;
    const projectReferences =
      presentation === "references" ? createChatProjectReferences(message.evidence) : null;
    renderAnswerBody(body, message.content, citationTargets);
    if (projectCards) {
      body.append(projectCards);
    }
    if (projectReferences) {
      body.append(projectReferences);
    }
  } else {
    body.append(element("p", "", message.content));
  }
  item.append(body);
  return item;
}

function createChatOnboardingMessage() {
  const item = createChatMessage(
    { role: "assistant", content: CHAT_ONBOARDING_MESSAGE },
    true,
  );
  item.classList.add("is-onboarding");
  item.setAttribute("aria-label", "RepoScout welcome");
  return item;
}

function createChatLoadingMessage(progressPhase = "working") {
  const item = element("article", "chat-message is-assistant is-pending");
  item.setAttribute("aria-label", "RepoScout is working");
  item.append(element("span", "chat-speaker", "RepoScout"));
  const indicator = element("div", "typing-indicator");
  const dots = element("span", "typing-dots");
  dots.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 3; index += 1) {
    dots.append(element("span"));
  }
  const copy = element(
    "span",
    "chat-progress-copy",
    ASSISTANT_PROGRESS_COPY[progressPhase] ?? ASSISTANT_PROGRESS_COPY.working,
  );
  copy.setAttribute("role", "status");
  copy.setAttribute("aria-live", "polite");
  copy.setAttribute("aria-atomic", "true");
  indicator.append(dots, copy);
  item.append(indicator);
  return item;
}

function updateChatProgress(progressPhase, customCopy = null) {
  const copy = document.querySelector(".chat-progress-copy");
  if (!copy) {
    return;
  }
  copy.textContent = customCopy ?? ASSISTANT_PROGRESS_COPY[progressPhase] ?? ASSISTANT_PROGRESS_COPY.working;
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function scrollChatToLatest(startAtTop = false, alignLatestAssistantStart = false) {
  const transcript = document.querySelector("#ask-transcript");
  if (!transcript) {
    return;
  }
  window.requestAnimationFrame(() => {
    let top = startAtTop ? 0 : transcript.scrollHeight;
    if (alignLatestAssistantStart) {
      const assistantMessages = transcript.querySelectorAll(
        ".chat-message.is-assistant:not(.is-pending)",
      );
      const latest = assistantMessages.item(assistantMessages.length - 1);
      if (latest) {
        const transcriptRect = transcript.getBoundingClientRect();
        const messageRect = latest.getBoundingClientRect();
        top = transcript.scrollTop + messageRect.top - transcriptRect.top - 12;
      }
    }
    transcript.scrollTo({
      top,
      behavior: prefersReducedMotion() ? "auto" : "smooth",
    });
  });
}

function renderChatTranscript(
  pendingUserMessage = null,
  { animateTail = 0, progressPhase = "working", alignLatestAssistantStart = false } = {},
) {
  const transcript = document.querySelector("#ask-transcript");
  const fragment = document.createDocumentFragment();
  const visibleMessages = [...chatState.messages];
  if (pendingUserMessage) {
    visibleMessages.push({ role: "user", content: pendingUserMessage });
  }
  if (visibleMessages.length === 0) {
    fragment.append(createChatOnboardingMessage());
  } else {
    const animateFrom = Math.max(visibleMessages.length - animateTail, 0);
    for (const [index, message] of visibleMessages.entries()) {
      fragment.append(createChatMessage(message, index >= animateFrom, index));
    }
  }
  if (pendingUserMessage) {
    fragment.append(createChatLoadingMessage(progressPhase));
  }
  transcript.replaceChildren(fragment);
  scrollChatToLatest(visibleMessages.length === 0, alignLatestAssistantStart);
}

function resetChatConversation() {
  chatState.conversationId = null;
  chatState.messages = [];
  chatState.blocked = false;
  sessionStorage.removeItem(CHAT_SESSION_KEY);
  renderChatTranscript();
  setStatus(document.querySelector("#ask-status"), "New chat started.", "success");
  const query = document.querySelector("#ask-query");
  query.disabled = false;
  query.dispatchEvent(new Event("input"));
  query.focus({ preventScroll: true });
}

function validateAssistantMessage(message) {
  if (!message) {
    return "Tell RepoScout what you want to find or do.";
  }
  if (message.length > 2000) {
    return "Keep your message to 2,000 characters or fewer.";
  }
  return null;
}

function assistantErrorMessage(error) {
  if (error instanceof ApiError && error.status === 409) {
    return "RepoScout is already working on this chat. Wait for it to finish.";
  }
  if (error instanceof ApiError && error.status === 410) {
    return "This chat has expired. Start a new chat to continue.";
  }
  if (error instanceof ApiError && error.status === 422) {
    return "Check your message and try again.";
  }
  if (error instanceof ApiError && !error.uncertain && error.status === 503) {
    return "Ask RepoScout is temporarily unavailable. Discover is still available.";
  }
  if (error instanceof ApiError && !error.uncertain && error.status === 504) {
    return "Ask RepoScout timed out before any project changes started. You can try again.";
  }
  if (error instanceof ApiError && !error.uncertain && error.status === 502) {
    return "Ask RepoScout could not complete the request. You can try again.";
  }
  return UNCERTAIN_COMPLETION_MESSAGE;
}

function setupAssistantChat() {
  const form = document.querySelector("#ask-form");
  const query = document.querySelector("#ask-query");
  const status = document.querySelector("#ask-status");
  const send = form.querySelector(".chat-send-button");
  const stop = form.querySelector(".chat-stop-button");
  const reset = document.querySelector("#new-conversation");
  const restart = document.querySelector("#chat-restart");
  let controller = null;
  let activeTurnId = null;
  let cancellationPromise = null;
  let composing = false;
  let requestActive = false;

  function resizeComposer() {
    query.style.height = "auto";
    const nextHeight = Math.min(query.scrollHeight, MAX_CHAT_COMPOSER_HEIGHT);
    query.style.height = `${nextHeight}px`;
    query.style.overflowY = query.scrollHeight > MAX_CHAT_COMPOSER_HEIGHT ? "auto" : "hidden";
  }

  function syncComposerControls() {
    const hasMessage = query.value.trim().length > 0;
    const canSend = hasMessage && !requestActive && !chatState.blocked;
    send.hidden = !canSend;
    send.disabled = !canSend;
    stop.hidden = !requestActive;
    query.disabled = chatState.blocked;
    reset.disabled = requestActive;
    restart.hidden = !chatState.blocked;
    form.setAttribute("aria-busy", String(requestActive));
  }

  function restoreDraft(message) {
    if (!query.value.trim()) {
      query.value = message;
    }
    resizeComposer();
  }

  restoreChatSession();
  renderChatTranscript();
  resizeComposer();
  syncComposerControls();
  reset.addEventListener("click", resetChatConversation);
  restart.addEventListener("click", resetChatConversation);
  stop.addEventListener("click", () => {
    if (!controller || !activeTurnId || cancellationPromise) {
      return;
    }
    stop.disabled = true;
    updateChatProgress("working", "Stopping…");
    const activeController = controller;
    const turnId = activeTurnId;
    cancellationPromise = apiRequest(`assistant/turns/${encodeURIComponent(turnId)}/cancel`, {
      method: "POST",
    })
      .catch(() => ({ outcome: "uncertain", result: null }))
      .finally(() => activeController.abort());
  });
  query.addEventListener("input", () => {
    resizeComposer();
    syncComposerControls();
  });
  query.addEventListener("compositionstart", () => {
    composing = true;
  });
  query.addEventListener("compositionend", () => {
    composing = false;
    resizeComposer();
    syncComposerControls();
  });
  query.addEventListener("keydown", (event) => {
    if (
      event.key !== "Enter" ||
      event.shiftKey ||
      event.isComposing ||
      composing ||
      event.keyCode === 229
    ) {
      return;
    }
    event.preventDefault();
    if (query.value.trim() && !requestActive && !chatState.blocked) {
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (controller !== null) {
      return;
    }
    if (chatState.blocked) {
      setStatus(
        status,
        "Start a new chat before sending another message. Your draft has been preserved.",
        "error",
      );
      document.querySelector("#new-conversation").focus({ preventScroll: true });
      return;
    }

    const message = query.value.trim();
    const validationMessage = validateAssistantMessage(message);
    query.setAttribute("aria-invalid", validationMessage ? "true" : "false");
    if (validationMessage) {
      setStatus(status, validationMessage, "error");
      query.focus();
      return;
    }

    controller = new AbortController();
    activeTurnId = createTurnId();
    cancellationPromise = null;
    requestActive = true;
    query.value = "";
    resizeComposer();
    syncComposerControls();
    renderChatTranscript(message, { animateTail: 1 });
    setStatus(status, "");

    function acceptResponse(response) {
      chatState.conversationId = response.conversation_id;
      chatState.messages.push(
        { role: "user", content: message },
        {
          role: "assistant",
          content: response.message.content,
          presentation: ["cards", "references", "text"].includes(
            response.message.presentation,
          )
            ? response.message.presentation
            : Array.isArray(response.message.evidence) && response.message.evidence.length > 0
              ? "cards"
              : "text",
          evidence: Array.isArray(response.message.evidence) ? response.message.evidence : [],
        },
      );
      chatState.messages = chatState.messages.slice(-MAX_VISIBLE_CHAT_MESSAGES);
      persistChatSession();
      renderChatTranscript(null, { animateTail: 2, alignLatestAssistantStart: true });
      setStatus(status, "");
    }

    try {
      const response = await assistantStreamRequest(
        {
          turn_id: activeTurnId,
          conversation_id: chatState.conversationId,
          message,
        },
        {
          signal: controller.signal,
          onProgress: (phase) => updateChatProgress(phase),
        },
      );
      acceptResponse(response);
    } catch (error) {
      renderChatTranscript();
      restoreDraft(message);
      const stopped = cancellationPromise !== null;
      const cancellation = stopped ? await cancellationPromise : null;
      if (cancellation?.outcome === "completed" && cancellation.result) {
        acceptResponse(cancellation.result);
        return;
      }
      const safelyCancelled = cancellation?.outcome === "cancelled";
      const uncertainCancellation = stopped && !safelyCancelled;
      const uncertain =
        uncertainCancellation || (!stopped && !(error instanceof ApiError)) || error?.uncertain;
      if (uncertain || (error instanceof ApiError && error.status === 410)) {
        chatState.blocked = true;
      }
      if (safelyCancelled) {
        setStatus(status, SAFE_CANCELLATION_MESSAGE, "cancelled");
      } else if (uncertainCancellation) {
        setStatus(status, CANCELLED_COMPLETION_MESSAGE, "error");
      } else {
        setStatus(status, assistantErrorMessage(error), "error");
      }
    } finally {
      controller = null;
      activeTurnId = null;
      cancellationPromise = null;
      requestActive = false;
      stop.disabled = false;
      resizeComposer();
      syncComposerControls();
      if (chatState.blocked) {
        restart.focus({ preventScroll: true });
      } else {
        query.focus({ preventScroll: true });
      }
    }
  });
}

function savedProjectStatusLabel(value) {
  return {
    INTERESTED: "Interested",
    TO_TRY: "To try",
    IN_PROGRESS: "In progress",
    COMPLETED: "Completed",
  }[value] ?? "Saved";
}

function formatProjectDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Date unavailable";
  }
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

function createSavedProjectCard(project) {
  const card = element("article", "project-card saved-project-card is-entering");
  card.dataset.repoId = String(project.repo_id ?? "");
  const header = element("div", "project-header");
  header.append(element("h3", "", project.full_name || project.name || "Unnamed repository"));
  const githubUrl = safeGitHubUrl(project.html_url);
  if (githubUrl) {
    const link = element("a", "github-link", "View on GitHub ↗");
    link.href = githubUrl.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    header.append(link);
  }
  card.append(header);
  card.append(
    element(
      "p",
      "project-description",
      project.description || "No project description is currently available.",
    ),
  );

  const metadata = element("div", "metadata-row");
  metadata.append(element("span", `status-badge status-${project.status.toLowerCase()}`, savedProjectStatusLabel(project.status)));
  if (project.primary_language) {
    metadata.append(metadataChip(project.primary_language));
  }
  metadata.append(metadataChip(`★ ${formatCount(project.stars)} stars`));
  metadata.append(metadataChip(`${formatCount(project.forks)} forks`));
  if (project.license) {
    metadata.append(metadataChip(project.license));
  }
  card.append(metadata);
  card.append(
    element(
      "p",
      "saved-project-dates",
      `Saved ${formatProjectDate(project.saved_at)} · Updated ${formatProjectDate(project.updated_at)}`,
    ),
  );

  const notes = Array.isArray(project.notes) ? project.notes : [];
  if (notes.length > 0) {
    const details = element("details", "project-notes");
    details.append(element("summary", "", `Notes (${notes.length})`));
    const list = element("ul", "project-note-list");
    for (const note of notes) {
      const item = element("li");
      item.append(
        element("p", "", note.note),
        element("time", "", formatProjectDate(note.created_at)),
      );
      list.append(item);
    }
    details.append(list);
    card.append(details);
  } else {
    card.append(element("p", "project-notes-empty", "No notes yet."));
  }

  const actions = element("div", "saved-project-actions");
  const remove = element(
    "button",
    "secondary-button remove-saved-project-button",
    "Remove from My Projects",
  );
  remove.type = "button";
  remove.dataset.removeSavedProject = "";
  remove.setAttribute(
    "aria-label",
    `Remove ${project.full_name || project.name || "this project"} from My Projects`,
  );
  actions.append(remove);
  card.append(actions);
  return card;
}

let projectsController = null;

function showSavedProjectsEmptyState(results) {
  const empty = element("div", "empty-panel");
  empty.append(
    element("h3", "", "No saved projects yet"),
    element("p", "", "Ask RepoScout to find a project, then tell it to save your choice."),
  );
  const link = element("a", "secondary-button", "Ask RepoScout");
  link.href = "#ask";
  empty.append(link);
  results.replaceChildren(empty);
}

async function loadSavedProjects() {
  if (projectsController !== null) {
    return;
  }
  const status = document.querySelector("#projects-status");
  const results = document.querySelector("#projects-results");
  const refresh = document.querySelector("#projects-refresh");
  projectsController = new AbortController();
  refresh.disabled = true;
  showLoading(results, 2);
  setStatus(status, "Loading your saved projects…");
  try {
    const response = await apiRequest("saved-projects", { signal: projectsController.signal });
    const projects = Array.isArray(response.projects) ? response.projects : [];
    if (projects.length === 0) {
      showSavedProjectsEmptyState(results);
    } else {
      const fragment = document.createDocumentFragment();
      for (const project of projects) {
        fragment.append(createSavedProjectCard(project));
      }
      results.replaceChildren(fragment);
    }
    setStatus(status, "");
  } catch (error) {
    if (!(error instanceof DOMException && error.name === "AbortError")) {
      results.replaceChildren();
      setStatus(status, friendlyError(error), "error");
    }
  } finally {
    projectsController = null;
    refresh.disabled = false;
    results.setAttribute("aria-busy", "false");
  }
}

function setupSavedProjectRemoval() {
  const dialog = document.querySelector("#remove-saved-project-dialog");
  const projectName = document.querySelector("#remove-saved-project-name");
  const dialogStatus = document.querySelector("#remove-saved-project-status");
  const cancel = document.querySelector("#remove-saved-project-cancel");
  const confirm = document.querySelector("#remove-saved-project-confirm");
  const confirmLabel = confirm.querySelector(".button-label");
  const results = document.querySelector("#projects-results");
  const projectsStatus = document.querySelector("#projects-status");
  const projectsTitle = document.querySelector("#projects-title");
  let selection = null;
  let removing = false;

  function setRemoving(active) {
    removing = active;
    cancel.disabled = active;
    confirm.disabled = active;
    confirm.classList.toggle("is-loading", active);
    confirmLabel.textContent = active ? "Removing…" : "Remove from My Projects";
    dialog.setAttribute("aria-busy", String(active));
  }

  function resetDialog() {
    selection = null;
    projectName.textContent = "";
    dialogStatus.textContent = "";
    dialogStatus.classList.remove("is-error");
    setRemoving(false);
  }

  function focusAfterRemoval(card) {
    const cards = [...results.querySelectorAll(".saved-project-card")];
    const index = cards.indexOf(card);
    const adjacent = cards[index + 1] ?? cards[index - 1] ?? null;
    const target = adjacent?.querySelector(".remove-saved-project-button") ?? projectsTitle;
    window.requestAnimationFrame(() => target.focus({ preventScroll: true }));
  }

  function reconcileRemoval(card, message) {
    focusAfterRemoval(card);
    card.remove();
    if (!results.querySelector(".saved-project-card")) {
      showSavedProjectsEmptyState(results);
    }
    dialog.close("removed");
    setStatus(projectsStatus, message, "success");
  }

  results.addEventListener("click", (event) => {
    const trigger = event.target.closest?.("[data-remove-saved-project]");
    if (!trigger || removing) {
      return;
    }
    const card = trigger.closest(".saved-project-card");
    const repoId = Number(card?.dataset.repoId);
    const fullName = card?.querySelector("h3")?.textContent?.trim();
    if (!card || !Number.isInteger(repoId) || repoId < 1 || !fullName) {
      return;
    }
    selection = { card, fullName, repoId, trigger };
    projectName.textContent = fullName;
    dialogStatus.textContent = "";
    dialogStatus.classList.remove("is-error");
    setStatus(projectsStatus, "");
    dialog.showModal();
    cancel.focus({ preventScroll: true });
  });

  cancel.addEventListener("click", () => {
    if (!removing) {
      dialog.close("cancelled");
    }
  });

  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (!removing) {
      dialog.close("cancelled");
    }
  });

  dialog.addEventListener("close", () => {
    const trigger = selection?.trigger;
    const restoreTrigger = dialog.returnValue === "cancelled" && trigger?.isConnected;
    resetDialog();
    if (restoreTrigger) {
      trigger.focus({ preventScroll: true });
    }
  });

  confirm.addEventListener("click", async () => {
    if (!selection || removing) {
      return;
    }
    const current = selection;
    setRemoving(true);
    dialogStatus.classList.remove("is-error");
    dialogStatus.textContent = "Removing saved project and its notes…";
    try {
      await apiRequest(`saved-projects/${encodeURIComponent(current.repoId)}`, {
        method: "DELETE",
      });
      reconcileRemoval(
        current.card,
        `${current.fullName} was removed from My Projects with its saved notes.`,
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        reconcileRemoval(current.card, "This project was already removed from My Projects.");
        return;
      }
      dialogStatus.classList.add("is-error");
      dialogStatus.textContent = friendlyError(error);
    } finally {
      if (dialog.open) {
        setRemoving(false);
      }
    }
  });
}

function activeSearchQuery() {
  const queryId = activeViewFromHash() === "ask" ? "#ask-query" : "#discover-query";
  return document.querySelector(queryId).value.trim();
}

function clearIndexingRequestStatus() {
  const status = document.querySelector("#indexing-request-status");
  delete status.dataset.completed;
  setStatus(status, "");
}

function openCoveragePanel(prefill = "") {
  const panel = document.querySelector("#coverage-panel");
  const toggle = document.querySelector("#coverage-toggle");
  const query = document.querySelector("#indexing-search-query");
  const status = document.querySelector("#indexing-request-status");

  if (status.dataset.completed === "true") {
    clearIndexingRequestStatus();
  }
  panel.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
  if (!query.value.trim() && prefill.trim()) {
    query.value = prefill.trim();
  }
  query.focus({ preventScroll: true });
}

function dismissCoveragePanel() {
  document.querySelector("#coverage-panel").hidden = true;
  document.querySelector("#coverage-toggle").setAttribute("aria-expanded", "false");
  clearIndexingRequestStatus();
  document.querySelector("#coverage-toggle").focus({ preventScroll: true });
}

function validateIndexingRequest(form) {
  const query = form.querySelector("textarea[name='search_query']");
  const notes = form.querySelector("textarea[name='notes']");
  const searchQuery = query.value.trim();
  const context = notes.value.trim();
  query.setAttribute("aria-invalid", "false");
  notes.setAttribute("aria-invalid", "false");

  if (!searchQuery) {
    query.setAttribute("aria-invalid", "true");
    query.focus();
    return { error: "Describe what you were hoping to find." };
  }
  if (searchQuery.length > 500) {
    query.setAttribute("aria-invalid", "true");
    query.focus();
    return { error: "Keep the requested topic to 500 characters or fewer." };
  }
  if (context.length > 2000) {
    notes.setAttribute("aria-invalid", "true");
    notes.focus();
    return { error: "Keep the additional context to 2,000 characters or fewer." };
  }
  return {
    payload: {
      search_query: searchQuery,
      notes: context || null,
    },
  };
}

function setIndexingRequestLoading(form, loading) {
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = loading;
  submit.classList.toggle("is-loading", loading);
  form.setAttribute("aria-busy", String(loading));
  for (const control of form.querySelectorAll("textarea")) {
    control.disabled = loading;
  }
}

function setupIndexingRequest() {
  const form = document.querySelector("#indexing-request-form");
  const status = document.querySelector("#indexing-request-status");
  const toggle = document.querySelector("#coverage-toggle");
  let submitting = false;

  toggle.addEventListener("click", () => {
    if (document.querySelector("#coverage-panel").hidden) {
      openCoveragePanel(activeSearchQuery());
    } else {
      dismissCoveragePanel();
    }
  });
  document.querySelector("#coverage-dismiss").addEventListener("click", dismissCoveragePanel);
  form.addEventListener("input", () => {
    if (status.dataset.completed === "true") {
      clearIndexingRequestStatus();
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) {
      return;
    }
    if (status.dataset.completed === "true") {
      clearIndexingRequestStatus();
    }

    const validation = validateIndexingRequest(form);
    if (validation.error) {
      setStatus(status, validation.error, "error");
      return;
    }

    submitting = true;
    delete status.dataset.completed;
    setIndexingRequestLoading(form, true);
    setStatus(status, "Submitting your request for review…");
    try {
      await apiRequest("indexing-requests", {
        method: "POST",
        body: JSON.stringify(validation.payload),
      });
      form.reset();
      setStatus(status, "Request received for review.", "success");
      status.dataset.completed = "true";
      status.setAttribute("tabindex", "-1");
      status.focus({ preventScroll: true });
    } catch (error) {
      setStatus(status, friendlyError(error), "error");
    } finally {
      submitting = false;
      setIndexingRequestLoading(form, false);
    }
  });
}

function setRequestLoading(form, loading) {
  const submit = form.querySelector("button[type='submit']");
  const cancel = form.querySelector(".cancel-button");
  submit.disabled = loading;
  submit.classList.toggle("is-loading", loading);
  form.setAttribute("aria-busy", String(loading));
  for (const control of form.querySelectorAll("textarea, input")) {
    control.disabled = loading;
  }
  cancel.hidden = !loading;
}

function setupDiscoverSearch() {
  const form = document.querySelector("#discover-form");
  const status = document.querySelector("#discover-status");
  const results = document.querySelector("#discover-results");
  const cancel = form.querySelector(".cancel-button");
  const submit = form.querySelector("button[type='submit']");
  let controller = null;

  cancel.addEventListener("click", () => controller?.abort());

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (controller !== null) {
      return;
    }

    const payload = buildSearchPayload(form);
    const validationMessage = validateSearchForm(form, payload);
    if (validationMessage) {
      setStatus(status, validationMessage, "error");
      return;
    }

    controller = new AbortController();
    setRequestLoading(form, true);
    results.setAttribute("aria-busy", "true");
    showLoading(results, 2);
    setStatus(status, "Searching repositories…");
    window.requestAnimationFrame(() => scrollElementIntoView(results));

    try {
      const response = await apiRequest("search/semantic", {
        method: "POST",
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      renderProjects(response.projects, results, {
        emptyTitle: "No matching projects yet",
        emptyMessage: "Try broader wording, another language, or a lower star filter.",
        coverageQuery: payload.query,
      });
      setStatus(status, "");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        results.replaceChildren();
        setStatus(status, "Request cancelled. Your search is ready to edit or retry.", "cancelled");
      } else {
        results.replaceChildren();
        setStatus(status, friendlyError(error), "error");
      }
    } finally {
      controller = null;
      results.setAttribute("aria-busy", "false");
      setRequestLoading(form, false);
      submit.focus({ preventScroll: true });
    }
  });
}

document.querySelector("#corpus-retry").addEventListener("click", loadCorpusSummary);
document.querySelector("#projects-refresh").addEventListener("click", loadSavedProjects);
window.addEventListener("hashchange", () =>
  showView(activeViewFromHash(), { moveFocus: true, scrollToWorkspace: true }),
);

setupPrimaryNavigation();
normalizeInitialHash();
setupExampleQueries();
setupIndexingRequest();
setupAssistantChat();
setupDiscoverSearch();
setupSavedProjectRemoval();
loadCorpusSummary();
