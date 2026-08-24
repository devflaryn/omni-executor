/* Lua tokenizer + HTML highlighter (spans use the .tok-* component classes). */

const LUA_KEYWORDS = new Set([
  "and", "break", "do", "else", "elseif", "end", "false", "for", "function",
  "goto", "if", "in", "local", "nil", "not", "or", "repeat", "return",
  "then", "true", "until", "while",
]);

const LUA_BUILTINS = new Set([
  "print", "pairs", "ipairs", "next", "type", "tostring", "tonumber",
  "require", "pcall", "xpcall", "error", "assert", "select", "unpack",
  "rawget", "rawset", "rawequal", "rawlen", "setmetatable", "getmetatable",
  "collectgarbage", "load", "loadstring", "dofile", "coroutine", "table",
  "string", "math", "os", "io", "utf8", "debug", "_G", "_ENV",
  // Roblox / Luau globals the scripts here actually use.
  "game", "workspace", "script", "wait", "task", "spawn", "delay", "tick",
  "Instance", "Vector3", "Vector2", "CFrame", "Color3", "UDim2", "Enum",
  "typeof", "warn", "getgenv", "getrenv", "loadstring",
]);

const TOKEN_RE = new RegExp(
  [
    "(?<comment>--\\[(?<ceq>=*)\\[[\\s\\S]*?(?:\\]\\k<ceq>\\]|$)|--[^\\n]*)",
    "(?<string>\\[(?<seq>=*)\\[[\\s\\S]*?(?:\\]\\k<seq>\\]|$)|\"(?:\\\\.|[^\"\\\\\\n])*\"?|'(?:\\\\.|[^'\\\\\\n])*'?)",
    "(?<number>0[xX][0-9a-fA-F]+(?:\\.[0-9a-fA-F]*)?(?:[pP][+-]?\\d+)?|\\d+\\.?\\d*(?:[eE][+-]?\\d+)?|\\.\\d+(?:[eE][+-]?\\d+)?)",
    "(?<word>[A-Za-z_]\\w*)",
    "(?<op>==|~=|<=|>=|\\.\\.\\.?|[+\\-*/%^#=<>])",
  ].join("|"),
  "g"
);

export function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const SPACE = /\s/;

function prevChar(source, index) {
  let i = index - 1;
  while (i >= 0 && SPACE.test(source[i])) i -= 1;
  return i >= 0 ? source[i] : "";
}

function nextChar(source, index) {
  let i = index;
  while (i < source.length && SPACE.test(source[i])) i += 1;
  return i < source.length ? source[i] : "";
}

/** Decide what an identifier IS from its neighbours, the way a reader does:
    a name followed by a call is a function, a name after `.` or `:` is a
    property, a name after `function` is being declared, anything else is a
    variable. Keywords and builtins are looked up first. */
export function classifyWord(word, source, start, end, lastWord) {
  if (LUA_KEYWORDS.has(word)) return "kw";
  const before = prevChar(source, start);
  const after = nextChar(source, end);
  // Call syntax, including Lua's sugar for a single string/table argument.
  const called = after === "(" || after === "{" || after === '"' || after === "'";
  if (before === "." || before === ":") return called ? "fn" : "prop";
  // `function M.new(` declares `new`; `M` is the table it lives in.
  if (lastWord === "function") return after === "." || after === ":" ? "var" : "fn";
  if (called) return "fn";
  if (LUA_BUILTINS.has(word)) return "fn";
  return "var";
}

export function highlightLua(source) {
  let html = "";
  let last = 0;
  let lastWord = null;
  TOKEN_RE.lastIndex = 0;
  for (const match of source.matchAll(TOKEN_RE)) {
    const gap = source.slice(last, match.index);
    html += escapeHtml(gap);
    // Any punctuation between two words (a `(`, a `,`) means the previous
    // word is no longer "the word before this one".
    if (/\S/.test(gap)) lastWord = null;
    const g = match.groups;
    const text = escapeHtml(match[0]);
    const end = match.index + match[0].length;
    if (g.comment !== undefined) html += `<span class="tok-com">${text}</span>`;
    else if (g.string !== undefined) html += `<span class="tok-str">${text}</span>`;
    else if (g.number !== undefined) html += `<span class="tok-num">${text}</span>`;
    else if (g.op !== undefined) html += `<span class="tok-op">${text}</span>`;
    else {
      const cls = classifyWord(match[0], source, match.index, end, lastWord);
      html += `<span class="tok-${cls}">${text}</span>`;
    }
    lastWord = g.word !== undefined ? match[0] : null;
    last = end;
  }
  html += escapeHtml(source.slice(last));
  return html + "\n"; // trailing newline keeps pre/textarea heights in sync
}

export const SAMPLE_SCRIPT = [
  "-- Omni Executor · sample script",
  "local Greeter = {}",
  "Greeter.__index = Greeter",
  "",
  "function Greeter.new(name)",
  "    local self = setmetatable({}, Greeter)",
  '    self.name = name or "world"',
  "    return self",
  "end",
  "",
  "function Greeter:say()",
  '    print(("Hello, %s!"):format(self.name))',
  "end",
  "",
  "--[[ Memoized Fibonacci ]]",
  "local memo = {}",
  "local function fib(n)",
  "    if n < 2 then return n end",
  "    if not memo[n] then",
  "        memo[n] = fib(n - 1) + fib(n - 2)",
  "    end",
  "    return memo[n]",
  "end",
  "",
  "for i = 1, 10 do",
  '    io.write(fib(i), " ")',
  "end",
  "",
  'Greeter.new("Omni"):say()',
  "",
].join("\n");
