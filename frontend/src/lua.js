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
  "string", "math", "os", "io", "utf8", "debug", "self", "_G", "_ENV",
]);

const TOKEN_RE = new RegExp(
  [
    "(?<comment>--\\[(?<ceq>=*)\\[[\\s\\S]*?(?:\\]\\k<ceq>\\]|$)|--[^\\n]*)",
    "(?<string>\\[(?<seq>=*)\\[[\\s\\S]*?(?:\\]\\k<seq>\\]|$)|\"(?:\\\\.|[^\"\\\\\\n])*\"?|'(?:\\\\.|[^'\\\\\\n])*'?)",
    "(?<number>0[xX][0-9a-fA-F]+(?:\\.[0-9a-fA-F]*)?(?:[pP][+-]?\\d+)?|\\d+\\.?\\d*(?:[eE][+-]?\\d+)?|\\.\\d+(?:[eE][+-]?\\d+)?)",
    "(?<word>[A-Za-z_]\\w*)",
  ].join("|"),
  "g"
);

export function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function highlightLua(source) {
  let html = "";
  let last = 0;
  TOKEN_RE.lastIndex = 0;
  for (const match of source.matchAll(TOKEN_RE)) {
    html += escapeHtml(source.slice(last, match.index));
    const g = match.groups;
    const text = escapeHtml(match[0]);
    if (g.comment !== undefined) html += `<span class="tok-com">${text}</span>`;
    else if (g.string !== undefined) html += `<span class="tok-str">${text}</span>`;
    else if (g.number !== undefined) html += `<span class="tok-num">${text}</span>`;
    else if (LUA_KEYWORDS.has(match[0])) html += `<span class="tok-kw">${text}</span>`;
    else if (LUA_BUILTINS.has(match[0])) html += `<span class="tok-fn">${text}</span>`;
    else html += text;
    last = match.index + match[0].length;
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
