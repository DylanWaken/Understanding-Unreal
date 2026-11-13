(function () {
  "use strict";

  const doc = document;

  function text(value, fallback) {
    if (value === null || value === undefined || value === "") {
      return fallback || "";
    }
    return String(value);
  }

  function array(value) {
    return Array.isArray(value) ? value : [];
  }

  function safeRelativeUrl(value) {
    const url = text(value).trim();
    if (!url || url.startsWith("/") || url.startsWith("\\") || url.startsWith("//")) {
      return "";
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(url) || url.includes("\\")) {
      return "";
    }
    return url;
  }

  function setTheme(theme) {
    const resolved = theme === "dark" ? "dark" : "light";
    doc.documentElement.dataset.theme = resolved;
    doc.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.setAttribute("aria-label", "Use " + (resolved === "dark" ? "light" : "dark") + " theme");
      button.textContent = resolved === "dark" ? "Light theme" : "Dark theme";
    });
  }

  function prepareHeaderControls() {
    const header = doc.querySelector(".site-header");
    if (!header) {
      return;
    }
    const directNav = Array.from(header.children).find((child) => child.tagName === "NAV");
    if (!directNav) {
      return;
    }
    directNav.classList.add("site-nav");
    directNav.dataset.siteNav = "";
    if (!directNav.id) {
      directNav.id = "site-navigation";
    }
    if (!header.querySelector("[data-nav-toggle]")) {
      const navToggle = doc.createElement("button");
      navToggle.type = "button";
      navToggle.className = "nav-toggle";
      navToggle.dataset.navToggle = "";
      navToggle.setAttribute("aria-controls", directNav.id);
      navToggle.setAttribute("aria-expanded", "false");
      navToggle.textContent = "Menu";
      header.insertBefore(navToggle, directNav);
    }
    if (!header.querySelector("[data-theme-toggle]")) {
      const themeToggle = doc.createElement("button");
      themeToggle.type = "button";
      themeToggle.className = "theme-toggle";
      themeToggle.dataset.themeToggle = "";
      themeToggle.textContent = "Dark theme";
      header.appendChild(themeToggle);
    }
  }

  function initializeTheme() {
    let saved = "";
    try {
      saved = localStorage.getItem("understanding-unreal-theme") || "";
    } catch (_error) {
      saved = "";
    }
    const preferredDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    setTheme(saved || (preferredDark ? "dark" : "light"));

    doc.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const next = doc.documentElement.dataset.theme === "dark" ? "light" : "dark";
        setTheme(next);
        try {
          localStorage.setItem("understanding-unreal-theme", next);
        } catch (_error) {
          // Theme still applies when storage is unavailable.
        }
      });
    });
  }

  function initializeNavigation() {
    const toggle = doc.querySelector("[data-nav-toggle]");
    const nav = doc.querySelector("[data-site-nav]");
    if (toggle && nav) {
      toggle.addEventListener("click", () => {
        const open = nav.dataset.open !== "true";
        nav.dataset.open = String(open);
        toggle.setAttribute("aria-expanded", String(open));
      });

      nav.addEventListener("click", (event) => {
        if (event.target.closest("a")) {
          nav.dataset.open = "false";
          toggle.setAttribute("aria-expanded", "false");
        }
      });
    }

    const current = new URL(window.location.href);
    const currentPath = current.pathname.replace(/\/index\.html$/i, "/");
    doc.querySelectorAll("[data-site-nav] a").forEach((link) => {
      const target = new URL(link.href, current);
      const targetPath = target.pathname.replace(/\/index\.html$/i, "/");
      if (targetPath === currentPath) {
        link.setAttribute("aria-current", "page");
      }
    });
  }

  function initializeGlossaryTooltips() {
    const glossary = window.ArdaGlossary || {};
    let visibleTooltip = null;
    let visibleLink = null;
    let sequence = 0;

    function position(link, tooltip) {
      const margin = 8;
      const rect = link.getBoundingClientRect();
      const tooltipRect = tooltip.getBoundingClientRect();
      let left = rect.left + (rect.width - tooltipRect.width) / 2;
      left = Math.max(margin, Math.min(left, window.innerWidth - tooltipRect.width - margin));
      let top = rect.bottom + margin;
      if (top + tooltipRect.height > window.innerHeight - margin) {
        top = rect.top - tooltipRect.height - margin;
      }
      top = Math.max(margin, Math.min(top, window.innerHeight - tooltipRect.height - margin));
      tooltip.style.left = Math.round(left) + "px";
      tooltip.style.top = Math.round(top) + "px";
    }

    function hide(link) {
      if (link && visibleLink !== link) {
        return;
      }
      if (visibleTooltip) {
        visibleTooltip.hidden = true;
      }
      visibleTooltip = null;
      visibleLink = null;
    }

    doc.querySelectorAll("a[data-term]").forEach((link) => {
      const entry = glossary[link.dataset.term];
      if (!entry || !entry.definition) {
        return;
      }

      const tooltip = doc.createElement("span");
      tooltip.className = "term-tooltip";
      tooltip.id = "term-tooltip-" + (++sequence);
      tooltip.setAttribute("role", "tooltip");
      tooltip.textContent = entry.definition;
      tooltip.hidden = true;
      doc.body.appendChild(tooltip);

      const describedBy = (link.getAttribute("aria-describedby") || "").trim();
      link.setAttribute("aria-describedby", (describedBy + " " + tooltip.id).trim());

      const show = () => {
        if (visibleTooltip && visibleTooltip !== tooltip) {
          visibleTooltip.hidden = true;
        }
        visibleTooltip = tooltip;
        visibleLink = link;
        tooltip.hidden = false;
        position(link, tooltip);
      };

      link.addEventListener("mouseenter", show);
      link.addEventListener("mouseleave", () => {
        if (doc.activeElement !== link) {
          hide(link);
        }
      });
      link.addEventListener("focus", show);
      link.addEventListener("blur", () => hide(link));
    });

    doc.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        hide();
      }
    });
    window.addEventListener("resize", () => {
      if (visibleTooltip && visibleLink) {
        position(visibleLink, visibleTooltip);
      }
    });
    window.addEventListener("scroll", () => {
      // Keyboard focus may scroll the term into view after showing its tooltip.
      if (visibleTooltip && visibleLink && doc.activeElement === visibleLink) {
        position(visibleLink, visibleTooltip);
      } else {
        hide();
      }
    }, { passive: true });
  }

  function initializeGlossaryFilter() {
    const input = doc.querySelector("[data-glossary-filter]");
    const category = doc.querySelector("[data-glossary-category]");
    const entries = Array.from(doc.querySelectorAll("[data-glossary-entry]"));
    const status = doc.querySelector("[data-glossary-count]");
    const empty = doc.querySelector("[data-glossary-empty]");
    if (!input || !entries.length) {
      return;
    }

    function apply() {
      const query = input.value.trim().toLocaleLowerCase();
      const selectedCategory = category ? category.value : "";
      let shown = 0;
      entries.forEach((entry) => {
        const searchText = (entry.dataset.search || entry.textContent).toLocaleLowerCase();
        const matchesText = !query || searchText.includes(query);
        const matchesCategory = !selectedCategory || entry.dataset.category === selectedCategory;
        const matches = matchesText && matchesCategory;
        entry.hidden = !matches;
        if (matches) {
          shown += 1;
        }
      });
      if (status) {
        status.textContent = shown + " of " + entries.length + " terms shown";
      }
      if (empty) {
        empty.hidden = shown !== 0;
      }
    }

    input.addEventListener("input", apply);
    if (category) {
      category.addEventListener("change", apply);
    }
    apply();
  }

  const syntaxLanguages = {
    cpp: { label: "C++", aliases: ["c", "c++", "cc", "cpp", "cxx", "h", "hpp"] },
    hlsl: { label: "HLSL", aliases: ["hlsl", "shader"] },
    cmake: { label: "CMake", aliases: ["cmake"] },
    shell: { label: "Shell", aliases: ["bash", "console", "sh", "shell", "zsh"] },
    powershell: { label: "PowerShell", aliases: ["powershell", "ps1", "pwsh"] },
    javascript: { label: "JavaScript", aliases: ["javascript", "js", "mjs"] },
    json: { label: "JSON", aliases: ["json"] },
    yaml: { label: "YAML", aliases: ["yaml", "yml"] },
    markup: { label: "HTML / XML", aliases: ["html", "markup", "svg", "xml"] },
    output: { label: "Output", aliases: ["log", "output", "plaintext", "text", "txt"] }
  };

  const syntaxAliases = Object.keys(syntaxLanguages).reduce((aliases, language) => {
    syntaxLanguages[language].aliases.forEach((alias) => {
      aliases[alias] = language;
    });
    return aliases;
  }, {});

  function declaredCodeLanguage(code) {
    const pre = code.parentElement;
    const values = [
      code.dataset.language,
      pre && pre.dataset.language,
      ...Array.from(code.classList),
      ...(pre ? Array.from(pre.classList) : [])
    ];
    for (const value of values) {
      const candidate = text(value).toLocaleLowerCase().replace(/^language-/, "");
      if (syntaxAliases[candidate]) {
        return syntaxAliases[candidate];
      }
    }
    return "";
  }

  function detectCodeLanguage(source) {
    const sample = source.trim();
    if (!sample) {
      return "output";
    }
    if (/^(?:<\?xml\b|<!doctype\b|<!--|<svg\b|<\/?[a-z][\w:-]*(?:\s|>|\/>))/i.test(sample)) {
      return "markup";
    }
    if (/^[\[{]/.test(sample)) {
      try {
        JSON.parse(sample);
        return "json";
      } catch (_error) {
        // Braces also occur in C++, HLSL, and JavaScript.
      }
    }
    if (/(?:^|\n)\s*(?:cmake_minimum_required|project|add_(?:executable|library|subdirectory)|target_(?:link_libraries|include_directories|compile_definitions|sources)|find_package|set)\s*\(/i.test(sample)
        || /\$\{(?:CMAKE_|PROJECT_)[A-Z0-9_]*/.test(sample)) {
      return "cmake";
    }
    if (/\b(?:cbuffer|Texture[123]D|RWTexture[123]D|StructuredBuffer|RWStructuredBuffer|SamplerState|SV_(?:Position|Target|DispatchThreadID)|numthreads)\b/.test(sample)
        || /\b(?:float[234](?:x[234])?|half[234]?|uint[234]?)\s+\w+\s*(?::\s*[A-Z]\w*)?[;({=]/.test(sample)) {
      return "hlsl";
    }
    if (/(?:^|\n)\s*(?:PS [^>]*>|pwsh>|powershell>|Get-|Set-|New-|Remove-|Select-|Where-|ForEach-|Write-Host|Import-Module)\b/i.test(sample)
        || /\$[A-Za-z_]\w*\s*=/.test(sample) && /(?:^|\s)-[A-Za-z][\w-]*/.test(sample)) {
      return "powershell";
    }
    if (/(?:^|\n)\s*(?:\$|>)\s+\S/.test(sample)
        || /(?:^|\n)\s*(?:sudo |git |cmake |ninja|make(?:\s|$)|mkdir |cd |export |echo |python(?:3)? |node |npm |curl )/.test(sample)
        || /\\\s*\n/.test(sample) && /(?:^|\s)-{1,2}[\w-]+/.test(sample)) {
      return "shell";
    }
    if (/^(?:---\s*\n)?(?:[\w.-]+|["'][^"']+["']):(?:\s|$)/m.test(sample)
        && !/[;{}]\s*(?:\n|$)/.test(sample)) {
      return "yaml";
    }
    if (/\b(?:let|var|function|async|await|import|export|require)\b/.test(sample)
        || /\b(?:console|document|window)\s*\./.test(sample) || /=>|===|!==/.test(sample)) {
      return "javascript";
    }
    if (/^\s*#\s*(?:include|define|if|ifdef|ifndef|pragma)\b/m.test(sample)
        || /\b(?:class|struct|namespace|template|using|constexpr|static_cast|dynamic_cast|nullptr)\b/.test(sample)
        || /(?:->|::)|[;{}]\s*(?:\/\/[^\n]*)?(?:\n|$)/.test(sample)) {
      return "cpp";
    }
    return "output";
  }

  const syntaxWords = {
    cpp: {
      keyword: new Set(("alignas alignof asm auto break case catch class concept const consteval constexpr constinit const_cast continue co_await co_return co_yield decltype default delete do dynamic_cast else enum explicit export extern false for friend goto if inline mutable namespace new noexcept nullptr operator private protected public register reinterpret_cast requires return sizeof static static_assert static_cast struct switch template this thread_local throw true try typedef typeid typename union using virtual volatile while").split(" ")),
      type: new Set(("bool char char8_t char16_t char32_t double float int long short signed size_t uint8_t uint16_t uint32_t uint64_t unsigned void wchar_t string vector array map unordered_map unique_ptr shared_ptr weak_ptr optional variant").split(" ")),
      builtin: new Set(("std eastl assert move forward make_unique make_shared min max").split(" "))
    },
    hlsl: {
      keyword: new Set(("break case const continue default discard do else false for if in inout out return static switch true uniform while").split(" ")),
      type: new Set(("bool bool2 bool3 bool4 Buffer ByteAddressBuffer cbuffer double float float2 float3 float4 float2x2 float3x3 float4x4 half half2 half3 half4 int int2 int3 int4 matrix RWBuffer RWByteAddressBuffer RWStructuredBuffer RWTexture1D RWTexture2D RWTexture3D SamplerComparisonState SamplerState StructuredBuffer Texture1D Texture2D Texture3D TextureCube uint uint2 uint3 uint4 vector void").split(" ")),
      builtin: new Set(("abs all any asfloat asint asuint ceil clamp cos cross ddx ddy distance dot floor frac lerp length max min mul normalize pow reflect saturate sin smoothstep sqrt step").split(" "))
    },
    javascript: {
      keyword: new Set(("as async await break case catch class const continue debugger default delete do else export extends false finally for from function get if import in instanceof let new null of return set static super switch this throw true try typeof undefined var void while with yield").split(" ")),
      type: new Set(("Array BigInt Boolean Date Error Function Map Number Object Promise Proxy RegExp Set String Symbol WeakMap WeakSet").split(" ")),
      builtin: new Set(("console document globalThis JSON Math navigator window").split(" "))
    },
    json: {
      keyword: new Set(["false", "null", "true"]),
      type: new Set(),
      builtin: new Set()
    },
    cmake: {
      keyword: new Set(("AND BOOL BREAK CACHE COMMAND DEFINED ELSE ELSEIF ENDFOREACH ENDFUNCTION ENDIF ENDMACRO ENDWHILE EXISTS FALSE FOREACH FUNCTION IF IN_LIST MACRO MATCHES NOT OR PARENT_SCOPE PATH RETURN STREQUAL TRUE WHILE").toLocaleLowerCase().split(" ")),
      type: new Set(("FILEPATH INTERNAL PATH STRING").toLocaleLowerCase().split(" ")),
      builtin: new Set(("add_custom_command add_custom_target add_definitions add_executable add_library add_subdirectory cmake_minimum_required configure_file find_package include install list message option project set target_compile_definitions target_compile_features target_include_directories target_link_libraries target_sources").split(" "))
    },
    shell: {
      keyword: new Set(("case do done elif else esac fi for function if in then until while").split(" ")),
      type: new Set(),
      builtin: new Set(("awk cd chmod cmake cp curl echo env export git make mkdir mv ninja node npm printf python python3 pwd rm sed sudo tar test touch").split(" "))
    },
    powershell: {
      keyword: new Set(("begin break catch class continue data do dynamicparam else elseif end enum exit filter finally for foreach from function hidden if in param process return static switch throw trap try until using var while workflow").split(" ")),
      type: new Set(("bool byte char datetime decimal double float guid hashtable int long object psobject regex scriptblock string switch timespan uri version xml").split(" ")),
      builtin: new Set(("ForEach-Object Get-ChildItem Get-Command Get-Content Get-Item Import-Module Join-Path New-Item Out-File Remove-Item Select-Object Set-Content Set-Item Where-Object Write-Error Write-Host Write-Output").split(" "))
    },
    yaml: {
      keyword: new Set(["false", "null", "true", "yes", "no", "on", "off"]),
      type: new Set(),
      builtin: new Set()
    }
  };

  function appendSyntaxToken(fragment, value, kind) {
    if (!kind) {
      fragment.appendChild(doc.createTextNode(value));
      return;
    }
    const span = doc.createElement("span");
    span.className = "tok-" + kind;
    span.textContent = value;
    fragment.appendChild(span);
  }

  function tokenizeMarkup(source) {
    const fragment = doc.createDocumentFragment();
    const pattern = /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>|<![^>]*>|<\/?[A-Za-z][^>]*>|[^<]+|</g;
    let match;
    while ((match = pattern.exec(source))) {
      const value = match[0];
      if (value.startsWith("<!--")) {
        appendSyntaxToken(fragment, value, "comment");
      } else if (/^<\?|^<!/.test(value)) {
        appendSyntaxToken(fragment, value, "directive");
      } else if (value.startsWith("<")) {
        let offset = 0;
        const partPattern = /(<\/?|\/?>)|([A-Za-z_][\w:.-]*)(?=\s|\/?>|=)|("(?:\\.|[^"])*"|'(?:\\.|[^'])*')|(=)|(\s+)|([^=\s]+)/g;
        let part;
        while ((part = partPattern.exec(value))) {
          if (part.index > offset) {
            appendSyntaxToken(fragment, value.slice(offset, part.index));
          }
          const token = part[0];
          let kind = "";
          if (part[1] || part[5]) {
            kind = "operator";
          } else if (part[3]) {
            kind = "string";
          } else if (part[2]) {
            const before = value.slice(0, part.index);
            kind = /^(?:<\/?)?$/.test(before) ? "type" : "property";
          }
          appendSyntaxToken(fragment, token, kind);
          offset = part.index + token.length;
        }
        if (offset < value.length) {
          appendSyntaxToken(fragment, value.slice(offset));
        }
      } else {
        appendSyntaxToken(fragment, value);
      }
    }
    return fragment;
  }

  function tokenKind(language, value, source, index) {
    if (/^\s+$/.test(value)) {
      return "";
    }
    if (/^(?:\/\/|\/\*|#(?!\s*(?:include|define|if|ifdef|ifndef|elif|else|endif|pragma|error|warning)\b)|<!--)/.test(value)) {
      return "comment";
    }
    if (language === "cpp" && /^\s*#/.test(value)) {
      return "directive";
    }
    if (/^(?:"|'|`)/.test(value)) {
      if ((language === "json" || language === "yaml")
          && /^\s*:/.test(source.slice(index + value.length))) {
        return "property";
      }
      return "string";
    }
    if (/^(?:0[xX][\dA-Fa-f]+|0[bB][01]+|\d)/.test(value)) {
      return "number";
    }
    if (/^\$\{?|\$env:/i.test(value)) {
      return "variable";
    }
    if (/^[()[\]{}.,:;?~+\-*/%&|^!=<>]+$/.test(value)) {
      return "operator";
    }
    if (/^[A-Za-z_][\w-]*$/.test(value)) {
      const words = syntaxWords[language] || syntaxWords.cpp;
      const lookup = language === "cmake" ? value.toLocaleLowerCase() : value;
      if (words.keyword.has(lookup)) {
        return "keyword";
      }
      if (words.type.has(lookup) || (/^(?:F|E|I|T|U)[A-Z]\w+/.test(value) && (language === "cpp" || language === "hlsl"))) {
        return "type";
      }
      if (words.builtin.has(value) || words.builtin.has(lookup)) {
        return language === "shell" || language === "powershell" || language === "cmake" ? "command" : "builtin";
      }
      const before = source.slice(0, index);
      const after = source.slice(index + value.length);
      if ((language === "json" || language === "yaml") && /^\s*:/.test(after)) {
        return "property";
      }
      if (/(?:\.|->)\s*$/.test(before)) {
        return "property";
      }
      if (/^\s*\(/.test(after)) {
        return "function";
      }
      if ((language === "shell" || language === "powershell") && /(?:^|\n)\s*(?:[$>]\s*)?$/.test(before)) {
        return "command";
      }
    }
    return "";
  }

  function tokenizeCode(source, language) {
    if (language === "markup") {
      return tokenizeMarkup(source);
    }
    const fragment = doc.createDocumentFragment();
    if (language === "output") {
      fragment.appendChild(doc.createTextNode(source));
      return fragment;
    }
    const pattern = language === "cpp" || language === "hlsl"
      ? /^[ \t]*#[ \t]*(?:include|define|if|ifdef|ifndef|elif|else|endif|pragma|error|warning)\b[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\/\/[^\n]*|\/\*[\s\S]*?\*\/|\s+|\$\{[^}\n]*\}|\$[A-Za-z_]\w*|0[xX][\dA-Fa-f]+(?:[uUlL]*)|0[bB][01]+(?:[uUlL]*)|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[fFuUlL]*|[A-Za-z_][\w-]*|[()[\]{}.,:;?~+\-*/%&|^!=<>]+|./gm
      : /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\/\/[^\n]*|\/\*[\s\S]*?\*\/|#[^\n]*|\s+|\$\{[^}\n]*\}|\$env:[A-Za-z_]\w*|\$[A-Za-z_]\w*|0[xX][\dA-Fa-f]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z_][\w-]*|[()[\]{}.,:;?~+\-*/%&|^!=<>]+|./gm;
    let match;
    while ((match = pattern.exec(source))) {
      appendSyntaxToken(fragment, match[0], tokenKind(language, match[0], source, match.index));
    }
    return fragment;
  }

  function highlightCodeBlocks(root) {
    (root || doc).querySelectorAll("pre > code").forEach((code) => {
      if (code.dataset.syntaxHighlighted === "true") {
        return;
      }
      const source = code.textContent;
      const language = declaredCodeLanguage(code) || detectCodeLanguage(source);
      const labelText = syntaxLanguages[language].label;
      code.replaceChildren(tokenizeCode(source, language));
      code.dataset.syntaxHighlighted = "true";
      code.dataset.language = language;

      const pre = code.parentElement;
      pre.classList.add("syntax-highlighted");
      if (!pre.hasAttribute("aria-label")) {
        pre.setAttribute("aria-label", labelText + " code example");
      }
      if (!pre.querySelector(":scope > .code-language")) {
        const label = doc.createElement("span");
        label.className = "code-language";
        label.setAttribute("aria-hidden", "true");
        label.textContent = labelText;
        pre.appendChild(label);
      }
    });
  }

  function initializeCopyButtons(root) {
    (root || doc).querySelectorAll("pre > code").forEach((code) => {
      const pre = code.parentElement;
      if (pre.querySelector(".copy-code")) {
        return;
      }
      const button = doc.createElement("button");
      button.type = "button";
      button.className = "copy-code";
      button.textContent = "Copy";
      button.setAttribute("aria-label", "Copy code to clipboard");
      button.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(code.textContent);
          button.textContent = "Copied";
          window.setTimeout(() => {
            button.textContent = "Copy";
          }, 1600);
        } catch (_error) {
          button.textContent = "Copy failed";
        }
      });
      pre.appendChild(button);
    });
  }

  function initializeCodeBlocks(root) {
    highlightCodeBlocks(root);
    initializeCopyButtons(root);
  }

  function currentApiData() {
    return window.ArdaCurrentApi || window.ArdaRDGApi || window.ArdaBackendApi;
  }

  function apiSymbols() {
    const data = currentApiData();
    if (Array.isArray(data)) {
      return data;
    }
    if (data && Array.isArray(data.symbols)) {
      return data.symbols;
    }
    if (data && typeof data === "object") {
      return Object.values(data).filter((value) => value && typeof value === "object" && !Array.isArray(value));
    }
    return [];
  }

  function symbolName(symbol) {
    return text(symbol.qualifiedName || symbol.canonicalName || symbol.name, "Unnamed symbol");
  }

  function symbolComponent(symbol) {
    return text(symbol.component || symbol.group || symbol.module, "Other");
  }

  function componentLabel(component) {
    const data = currentApiData();
    const components = data && Array.isArray(data.components) ? data.components : [];
    const match = components.find((candidate) => text(candidate.id) === component);
    return match ? text(match.name, component) : component;
  }

  function symbolHref(symbol) {
    const explicit = safeRelativeUrl(symbol.href);
    if (explicit) {
      return explicit;
    }
    const page = safeRelativeUrl(symbol.page);
    const id = text(symbol.id).replace(/^#/, "");
    if (!page || !id) {
      return "";
    }
    return page + "#" + encodeURIComponent(id);
  }

  function appendMessage(container, message) {
    const paragraph = doc.createElement("p");
    paragraph.className = "callout";
    paragraph.textContent = message;
    container.replaceChildren(paragraph);
  }

  function symbolSearchText(symbol) {
    return [
      symbolName(symbol),
      symbol.name,
      symbol.kind,
      symbol.summary,
      symbol.details,
      symbolComponent(symbol),
      componentLabel(symbolComponent(symbol)),
      symbol.audience
    ].map((value) => text(value)).join(" ").toLocaleLowerCase();
  }

  function createApiFilterState() {
    const state = {
      query: "",
      component: "",
      kind: "",
      listeners: []
    };
    state.update = (changes, source) => {
      Object.assign(state, changes);
      state.listeners.forEach((listener) => listener(state, source));
    };
    return state;
  }

  function appendSelectOption(select, value, label) {
    const option = doc.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }

  function createApiFilterControls(symbols, state, prefix, includeSearch) {
    const controls = doc.createElement("div");
    controls.className = "api-controls";
    const inputs = {};

    function appendField(labelText, control) {
      const field = doc.createElement("div");
      field.className = "field";
      const label = doc.createElement("label");
      label.htmlFor = control.id;
      label.textContent = labelText;
      field.append(label, control);
      controls.appendChild(field);
    }

    if (includeSearch) {
      const search = doc.createElement("input");
      search.type = "search";
      search.id = prefix + "-search";
      search.placeholder = "Name, kind, summary, or audience";
      search.value = state.query;
      search.addEventListener("input", () => state.update({ query: search.value }, search));
      appendField("Search API symbols", search);
      inputs.search = search;
    }

    const component = doc.createElement("select");
    component.id = prefix + "-component";
    appendSelectOption(component, "", "All components");
    const componentCounts = new Map();
    symbols.forEach((symbol) => {
      const value = symbolComponent(symbol);
      componentCounts.set(value, (componentCounts.get(value) || 0) + 1);
    });
    Array.from(componentCounts.keys())
      .sort((left, right) => componentLabel(left).localeCompare(componentLabel(right)))
      .forEach((value) => appendSelectOption(component, value, componentLabel(value) + " (" + componentCounts.get(value) + ")"));
    component.value = state.component;
    component.addEventListener("change", () => state.update({ component: component.value }, component));
    appendField("Component", component);
    inputs.component = component;

    const kind = doc.createElement("select");
    kind.id = prefix + "-kind";
    appendSelectOption(kind, "", "All symbol kinds");
    const kindCounts = new Map();
    symbols.forEach((symbol) => {
      const value = text(symbol.kind, "other");
      kindCounts.set(value, (kindCounts.get(value) || 0) + 1);
    });
    Array.from(kindCounts.keys()).sort().forEach((value) => {
      appendSelectOption(kind, value, value + " (" + kindCounts.get(value) + ")");
    });
    kind.value = state.kind;
    kind.addEventListener("change", () => state.update({ kind: kind.value }, kind));
    appendField("Kind", kind);
    inputs.kind = kind;

    const status = doc.createElement("p");
    status.className = "filter-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    controls.appendChild(status);
    inputs.status = status;

    state.listeners.push((current, source) => {
      if (inputs.search && source !== inputs.search) {
        inputs.search.value = current.query;
      }
      if (source !== component) {
        component.value = current.component;
      }
      if (source !== kind) {
        kind.value = current.kind;
      }
    });
    return { controls, inputs };
  }

  function matchesApiFilter(symbol, state) {
    const query = state.query.trim().toLocaleLowerCase();
    return (!query || symbolSearchText(symbol).includes(query))
      && (!state.component || symbolComponent(symbol) === state.component)
      && (!state.kind || text(symbol.kind, "other") === state.kind);
  }

  function initializeApiIndexes(symbols, state) {
    doc.querySelectorAll("[data-api-index]").forEach((container, index) => {
      if (!symbols.length) {
        appendMessage(container, "API reference data is not available on this page.");
        return;
      }

      const matching = symbols.slice();
      if (!matching.length) {
        appendMessage(container, "No API symbols are available for this section.");
        return;
      }

      const generated = createApiFilterControls(matching, state, "api-index-" + index, true);
      const controls = generated.controls;
      const status = generated.inputs.status;

      const groups = doc.createElement("div");
      const items = [];
      const grouped = new Map();
      matching
        .slice()
        .sort((left, right) => symbolName(left).localeCompare(symbolName(right)))
        .forEach((symbol) => {
          const groupName = symbolComponent(symbol);
          if (!grouped.has(groupName)) {
            grouped.set(groupName, []);
          }
          grouped.get(groupName).push(symbol);
        });

      Array.from(grouped.keys()).sort().forEach((groupName) => {
        const section = doc.createElement("section");
        section.className = "api-group";
        const heading = doc.createElement("h3");
        heading.textContent = componentLabel(groupName);
        const list = doc.createElement("ul");
        list.className = "api-index-list";
        grouped.get(groupName).forEach((symbol) => {
          const item = doc.createElement("li");
          const href = symbolHref(symbol);
          const name = symbolName(symbol);
          if (href) {
            const link = doc.createElement("a");
            link.href = href;
            const nameNode = doc.createElement("span");
            nameNode.textContent = name;
            link.appendChild(nameNode);
            const kind = text(symbol.kind);
            if (kind) {
              const kindNode = doc.createElement("span");
              kindNode.className = "api-kind";
              kindNode.textContent = kind;
              link.appendChild(kindNode);
            }
            item.appendChild(link);
          } else {
            item.textContent = name;
          }
          list.appendChild(item);
          items.push({ item, section, symbol });
        });
        section.append(heading, list);
        groups.appendChild(section);
      });

      function filter() {
        let shown = 0;
        const sectionCounts = new Map();
        items.forEach(({ item, section, symbol }) => {
          const matches = matchesApiFilter(symbol, state);
          item.hidden = !matches;
          if (matches) {
            shown += 1;
            sectionCounts.set(section, (sectionCounts.get(section) || 0) + 1);
          }
        });
        groups.querySelectorAll(".api-group").forEach((section) => {
          section.hidden = !sectionCounts.get(section);
        });
        status.textContent = shown + " of " + items.length + " symbols shown";
      }

      state.listeners.push(filter);
      container.replaceChildren(controls, groups);
      filter();
    });
  }

  function initializeAuthoredApiSearch(state) {
    const form = doc.querySelector("form.api-search");
    const authoredInput = doc.querySelector("#api-search-input");
    const index = doc.querySelector("[data-api-index]");
    const generatedInput = index && index.querySelector('.api-controls input[type="search"]');
    if (!form || !authoredInput || !index || !generatedInput) {
      return;
    }

    const indexTarget = index.closest("#symbol-index") || index;
    authoredInput.setAttribute("aria-controls", indexTarget.id || generatedInput.id);

    function updateGeneratedSearch() {
      generatedInput.value = authoredInput.value;
      generatedInput.dispatchEvent(new Event("input", { bubbles: true }));
    }

    authoredInput.addEventListener("input", updateGeneratedSearch);
    generatedInput.addEventListener("input", () => {
      authoredInput.value = generatedInput.value;
    });
    if (state) {
      state.listeners.push((current) => {
        authoredInput.value = current.query;
      });
    }
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      updateGeneratedSearch();
      generatedInput.focus({ preventScroll: true });
      indexTarget.scrollIntoView({ block: "start" });
    });

    updateGeneratedSearch();
  }

  function appendDefinitionRow(list, label, value) {
    const rendered = text(value);
    if (!rendered) {
      return;
    }
    const term = doc.createElement("dt");
    term.textContent = label;
    const description = doc.createElement("dd");
    description.textContent = rendered;
    list.append(term, description);
  }

  function appendParameters(entry, params) {
    if (!params.length) {
      return;
    }
    const heading = doc.createElement("h4");
    heading.textContent = "Parameters";
    const wrapper = doc.createElement("div");
    wrapper.style.overflowX = "auto";
    const table = doc.createElement("table");
    const caption = doc.createElement("caption");
    caption.textContent = "Parameter contracts";
    const head = doc.createElement("thead");
    const headRow = doc.createElement("tr");
    ["Name", "Type", "Description"].forEach((value) => {
      const cell = doc.createElement("th");
      cell.scope = "col";
      cell.textContent = value;
      headRow.appendChild(cell);
    });
    head.appendChild(headRow);
    const body = doc.createElement("tbody");
    params.forEach((parameter) => {
      const row = doc.createElement("tr");
      const values = typeof parameter === "string"
        ? [parameter, "", ""]
        : [parameter.name, parameter.type, parameter.description || parameter.summary];
      values.forEach((value) => {
        const cell = doc.createElement("td");
        cell.textContent = text(value);
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
    table.append(caption, head, body);
    wrapper.appendChild(table);
    entry.append(heading, wrapper);
  }

  function appendRelated(entry, related) {
    if (!related.length) {
      return;
    }
    const heading = doc.createElement("h4");
    heading.textContent = "Related";
    const list = doc.createElement("ul");
    list.className = "related-links";
    related.forEach((relation) => {
      const item = doc.createElement("li");
      const label = typeof relation === "string" ? relation : text(relation.label || relation.name, "Related API");
      const href = typeof relation === "string" ? "" : safeRelativeUrl(relation.href || symbolHref(relation));
      if (href) {
        const link = doc.createElement("a");
        link.href = href;
        link.textContent = label;
        item.appendChild(link);
      } else {
        item.textContent = label;
      }
      list.appendChild(item);
    });
    entry.append(heading, list);
  }

  function renderApiEntry(symbol) {
    const entry = doc.createElement("section");
    entry.className = "api-entry";
    const id = text(symbol.id).replace(/^#/, "");
    if (id) {
      entry.id = id;
      entry.setAttribute("tabindex", "-1");
    }
    for (const alias of symbol.aliases || []) {
      const anchor = doc.createElement("span");
      anchor.id = alias;
      entry.appendChild(anchor);
    }
    const heading = doc.createElement("h5");
    heading.textContent = symbolName(symbol);
    entry.appendChild(heading);

    const signature = text(symbol.signature || symbol.declaration);
    if (signature) {
      const pre = doc.createElement("pre");
      const code = doc.createElement("code");
      code.textContent = signature;
      pre.appendChild(code);
      entry.appendChild(pre);
    }

    const summary = text(symbol.summary);
    if (summary) {
      const paragraph = doc.createElement("p");
      paragraph.textContent = summary;
      entry.appendChild(paragraph);
    }
    const details = text(symbol.details || symbol.description);
    if (details && details !== summary) {
      const paragraph = doc.createElement("p");
      paragraph.textContent = details;
      entry.appendChild(paragraph);
    }

    appendParameters(entry, array(symbol.params || symbol.parameters));
    const contract = doc.createElement("dl");
    appendDefinitionRow(contract, "Returns", symbol.returns || symbol.returnValue);
    appendDefinitionRow(contract, "Ownership", symbol.ownership);
    appendDefinitionRow(contract, "Errors", symbol.errors || symbol.failure);
    appendDefinitionRow(contract, "Threading", symbol.threading || symbol.synchronization);
    appendDefinitionRow(contract, "Audience", symbol.audience);
    appendDefinitionRow(contract, "Source", typeof symbol.source === "object" ? symbol.source.path || symbol.source.location : symbol.source);
    if (contract.children.length) {
      entry.appendChild(contract);
    }
    appendRelated(entry, array(symbol.related));
    return entry;
  }

  function apiGroupId(prefix, value) {
    return prefix + text(value, "other").toLocaleLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  function initializeApiReference(symbols, state) {
    doc.querySelectorAll("[data-api-reference]").forEach((container, index) => {
      if (!symbols.length) {
        appendMessage(container, "API reference data is not available on this page.");
        return;
      }

      const generated = createApiFilterControls(symbols, state, "api-reference-" + index, true);
      const groups = doc.createElement("div");
      const entries = [];
      const componentSections = [];
      const data = currentApiData();
      const knownComponents = array(data && data.components).map((component) => text(component.id));
      const componentIds = knownComponents.concat(
        Array.from(new Set(symbols.map(symbolComponent))).filter((component) => !knownComponents.includes(component)).sort()
      );

      componentIds.forEach((componentId) => {
        const componentSymbols = symbols.filter((symbol) => symbolComponent(symbol) === componentId);
        if (!componentSymbols.length) {
          return;
        }
        const componentSection = doc.createElement("section");
        componentSection.className = "api-group api-component-group";
        componentSection.id = apiGroupId("api-component-", componentId);
        componentSection.setAttribute("tabindex", "-1");
        const componentHeading = doc.createElement("h3");
        componentHeading.textContent = componentLabel(componentId) + " (" + componentSymbols.length + ")";
        componentSection.appendChild(componentHeading);

        const componentData = array(data && data.components)
          .find((candidate) => text(candidate.id) === componentId);
        if (componentData && componentData.summary) {
          const summary = doc.createElement("p");
          summary.textContent = text(componentData.summary);
          componentSection.appendChild(summary);
        }

        const kindSections = [];
        const kinds = Array.from(new Set(componentSymbols.map((symbol) => text(symbol.kind, "other")))).sort();
        kinds.forEach((kind) => {
          const kindSymbols = componentSymbols
            .filter((symbol) => text(symbol.kind, "other") === kind)
            .sort((left, right) => symbolName(left).localeCompare(symbolName(right)));
          const kindSection = doc.createElement("section");
          kindSection.className = "api-kind-group";
          kindSection.id = apiGroupId("api-kind-" + componentId + "-", kind);
          kindSection.setAttribute("tabindex", "-1");
          const kindHeading = doc.createElement("h4");
          kindHeading.textContent = kind + " (" + kindSymbols.length + ")";
          kindSection.appendChild(kindHeading);
          kindSymbols.forEach((symbol) => {
            const entry = renderApiEntry(symbol);
            kindSection.appendChild(entry);
            entries.push({ entry, symbol, kindSection, componentSection });
          });
          kindSections.push(kindSection);
          componentSection.appendChild(kindSection);
        });
        componentSections.push({ componentSection, kindSections });
        groups.appendChild(componentSection);
      });

      function filter() {
        let shown = 0;
        const kindCounts = new Map();
        const componentCounts = new Map();
        entries.forEach(({ entry, symbol, kindSection, componentSection }) => {
          const matches = matchesApiFilter(symbol, state);
          entry.hidden = !matches;
          if (matches) {
            shown += 1;
            kindCounts.set(kindSection, (kindCounts.get(kindSection) || 0) + 1);
            componentCounts.set(componentSection, (componentCounts.get(componentSection) || 0) + 1);
          }
        });
        componentSections.forEach(({ componentSection, kindSections }) => {
          componentSection.hidden = !componentCounts.get(componentSection);
          kindSections.forEach((kindSection) => {
            kindSection.hidden = !kindCounts.get(kindSection);
          });
        });
        generated.inputs.status.textContent = shown + " of " + entries.length + " detailed entries shown";
      }

      state.listeners.push(filter);
      container.replaceChildren(generated.controls, groups);
      filter();
      initializeCodeBlocks(container);
    });
  }

  function exactApiSymbol(symbols, requested) {
    if (!requested) {
      return null;
    }
    const exactQualified = symbols.find((symbol) => symbolName(symbol) === requested);
    const exactShort = symbols.find((symbol) => text(symbol.name) === requested);
    const folded = requested.toLocaleLowerCase();
    const foldedQualified = symbols.find((symbol) => symbolName(symbol).toLocaleLowerCase() === folded);
    const foldedShort = symbols.find((symbol) => text(symbol.name).toLocaleLowerCase() === folded);
    return exactQualified || exactShort || foldedQualified || foldedShort || null;
  }

  function appendApiDirectResult(symbol) {
    const form = doc.querySelector("form.api-search");
    if (!form || !symbol) {
      return;
    }
    const existing = doc.querySelector("[data-api-direct-result]");
    if (existing) {
      existing.remove();
    }
    const callout = doc.createElement("aside");
    callout.className = "callout";
    callout.dataset.apiDirectResult = "";
    callout.setAttribute("role", "region");
    callout.setAttribute("aria-label", "Direct API result");

    const heading = doc.createElement("h3");
    heading.textContent = "Direct API result";
    const description = doc.createElement("p");
    description.append("Exact match: ");
    const name = doc.createElement("strong");
    name.textContent = symbolName(symbol);
    description.append(
      name,
      " — " + text(symbol.kind, "symbol") + " in " + componentLabel(symbolComponent(symbol)) + "."
    );
    const action = doc.createElement("p");
    const link = doc.createElement("a");
    link.href = "#" + encodeURIComponent(text(symbol.id).replace(/^#/, ""));
    link.textContent = "Open canonical entry";
    action.appendChild(link);
    callout.append(heading, description, action);
    form.insertAdjacentElement("afterend", callout);
  }

  function suppressInitialApiFragment() {
    if (!window.location.hash) {
      return null;
    }
    let id;
    try {
      id = decodeURIComponent(window.location.hash.slice(1));
    } catch (_error) {
      id = window.location.hash.slice(1);
    }
    if (!/^api-/.test(id)) {
      return null;
    }
    try {
      const withoutFragment = new URL(window.location.href);
      withoutFragment.hash = "";
      window.history.replaceState(window.history.state, "", withoutFragment.href);
    } catch (_error) {
      // A direct-result callout still provides navigation if history is unavailable.
    }
    return { id };
  }

  function initializeApiDirectResult(symbols, state, initialFragment) {
    const params = new URLSearchParams(window.location.search);
    const requested = text(params.get("symbol")).trim();
    const queryMatch = exactApiSymbol(symbols, requested);
    const fragmentMatch = !queryMatch && initialFragment
      ? symbols.find((symbol) => text(symbol.id).replace(/^#/, "") === initialFragment.id)
      : null;
    const match = queryMatch || fragmentMatch;
    if (match) {
      state.update({
        query: requested || symbolName(match),
        component: symbolComponent(match),
        kind: text(match.kind, "other")
      });
      appendApiDirectResult(match);
    }
  }

  function initialize() {
    prepareHeaderControls();
    initializeTheme();
    initializeNavigation();
    doc.documentElement.classList.add("has-js");
    initializeGlossaryTooltips();
    initializeGlossaryFilter();
    initializeCodeBlocks();
    const initialApiFragment = suppressInitialApiFragment();
    const symbols = apiSymbols();
    const apiFilterState = createApiFilterState();
    initializeApiIndexes(symbols, apiFilterState);
    initializeAuthoredApiSearch(apiFilterState);
    initializeApiReference(symbols, apiFilterState);
    initializeApiDirectResult(symbols, apiFilterState, initialApiFragment);
  }

  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", initialize);
  } else {
    initialize();
  }
})();
