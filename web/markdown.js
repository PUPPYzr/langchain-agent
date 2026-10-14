/**
 * Minimal, dependency-free Markdown renderer with a small syntax highlighter.
 *
 * Security: all text is HTML-escaped before any markup is produced, and only a
 * fixed allow-list of tags is generated. Raw HTML from the model is therefore
 * never injected into the DOM.
 *
 * Scope note: this intentionally covers the subset the Agent actually emits
 * (headings, lists, blockquotes, tables, fenced/inline code, emphasis, links).
 * It is not a full CommonMark implementation.
 */

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ESCAPES[char]);
}

function rule(pattern, cls) {
  return { re: new RegExp(pattern, "y"), cls };
}

const LANGUAGES = {
  python: [
    rule("#[^\\n]*", "comment"),
    rule('"""[\\s\\S]*?"""', "string"),
    rule("'(?:\\\\.|[^'\\\\])*'", "string"),
    rule('"(?:\\\\.|[^"\\\\])*"', "string"),
    rule("\\b(?:def|class|return|import|from|if|elif|else|for|while|try|except|finally|with|as|in|not|and|or|None|True|False|lambda|yield|raise|async|await|pass|break|continue)\\b", "keyword"),
    rule("\\b\\d+(?:\\.\\d+)?\\b", "number"),
  ],
  javascript: [
    rule("//[^\\n]*", "comment"),
    rule("/\\*[\\s\\S]*?\\*/", "comment"),
    rule("`(?:\\\\.|[^`\\\\])*`", "string"),
    rule("'(?:\\\\.|[^'\\\\])*'", "string"),
    rule('"(?:\\\\.|[^"\\\\])*"', "string"),
    rule("\\b(?:const|let|var|function|return|if|else|for|while|await|async|import|export|from|new|class|try|catch|finally|typeof|instanceof|null|undefined|true|false)\\b", "keyword"),
    rule("\\b\\d+(?:\\.\\d+)?\\b", "number"),
  ],
  json: [
    rule('"(?:\\\\.|[^"\\\\])*"(?=\\s*:)', "keyword"),
    rule('"(?:\\\\.|[^"\\\\])*"', "string"),
    rule("\\b(?:true|false|null)\\b", "literal"),
    rule("-?\\b\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b", "number"),
  ],
  bash: [
    rule("#[^\\n]*", "comment"),
    rule("'(?:[^'])*'", "string"),
    rule('"(?:\\\\.|[^"\\\\])*"', "string"),
    rule("\\b(?:curl|python|npm|git|cd|set|export|echo)\\b", "keyword"),
    rule("\\s-{1,2}[A-Za-z][\\w-]*", "literal"),
  ],
};

function highlight(code, language) {
  const rules = LANGUAGES[language];
  if (!rules) return escapeHtml(code);
  let output = "";
  let index = 0;
  while (index < code.length) {
    let matched = false;
    for (const item of rules) {
      item.re.lastIndex = index;
      const match = item.re.exec(code);
      if (match && match.index === index) {
        output += `<span class="tok-${item.cls}">${escapeHtml(match[0])}</span>`;
        index += match[0].length;
        matched = true;
        break;
      }
    }
    if (!matched) {
      output += escapeHtml(code[index]);
      index += 1;
    }
  }
  return output;
}

function renderInline(text) {
  let html = escapeHtml(text);
  html = html.replace(/`([^`]+)`/g, '<code class="inline">$1</code>');
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  html = html.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
  );
  return html;
}

function renderTable(rows) {
  const [header, ...body] = rows;
  const head = header
    .split("|")
    .map((cell) => `<th>${renderInline(cell.trim())}</th>`)
    .join("");
  const rest = body
    .map(
      (row) =>
        `<tr>${row
          .split("|")
          .map((cell) => `<td>${renderInline(cell.trim())}</td>`)
          .join("")}</tr>`,
    )
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${rest}</tbody></table>`;
}

function codeBlock(code, language) {
  const label = language || "text";
  return (
    `<div class="code"><div class="code__head"><span>${escapeHtml(label)}</span>` +
    `<button type="button" class="copy-code" aria-label="复制代码">复制</button></div>` +
    `<pre><code>${highlight(code, language)}</code></pre></div>`
  );
}

/** Render Markdown text to a safe HTML string. */
export function renderMarkdown(text) {
  const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let paragraph = [];
  let listType = null;
  let tableRows = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${renderInline(paragraph.join(" "))}</p>`);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (listType) {
      out.push(`</${listType}>`);
      listType = null;
    }
  };
  const flushTable = () => {
    if (tableRows.length) {
      out.push(renderTable(tableRows));
      tableRows = [];
    }
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];

    const fence = /^\s*```(\w*)\s*$/.exec(line);
    if (fence) {
      flushParagraph();
      flushList();
      flushTable();
      const language = fence[1].toLowerCase();
      const body = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      out.push(codeBlock(body.join("\n"), language));
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line)) {
      flushParagraph();
      flushList();
      const cells = line.trim();
      if (!/^\|[\s:|-]+\|$/.test(cells)) tableRows.push(cells.replace(/^\||\|$/g, ""));
      continue;
    }
    flushTable();

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      const level = Math.min(3, heading[1].length);
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || ordered) {
      flushParagraph();
      const wanted = bullet ? "ul" : "ol";
      if (listType !== wanted) {
        flushList();
        out.push(`<${wanted}>`);
        listType = wanted;
      }
      out.push(`<li>${renderInline((bullet || ordered)[1])}</li>`);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      flushList();
      out.push(`<blockquote>${renderInline(line.replace(/^\s*>\s?/, ""))}</blockquote>`);
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  flushList();
  flushTable();
  return out.join("");
}
