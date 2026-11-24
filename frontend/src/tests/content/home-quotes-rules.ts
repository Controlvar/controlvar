/**
 * The rules `content/home-quotes.json` is held to: the Jest reader.
 *
 * Every homepage sentence that quotes a docs page is a row of that table
 * (#1459 SPEC A, which folds in #1415). A row is either a sentence,
 *
 *   {"id", "parts": [text | {"link", "page"}], "sources": [{"page", "phrases"}]}
 *
 * whose words are its parts joined, with at most one link, or a code block,
 *
 *   {"id", "code": [line], "sources": [{"page"}]}
 *
 * whose lines are each, once stripped, a line of a page it cites. A `page` is
 * a path under docs/ without `.md`.
 *
 * A sentence rests on one list item of docs/capabilities.md: the one that
 * holds every phrase it cites from that page. It says "beta" when any of these
 * holds, and only then:
 *
 *   - that item says "beta", anywhere in it;
 *   - the page the sentence links is titled "... (beta)";
 *   - the sentence contains the name of an item marked beta right after its
 *     link, as in "- [name](page.md) (full profile, beta)".
 *
 * "prior alpha and beta", a prior's parameters, does not count in an item or
 * a sentence. A code row is not checked for beta.
 *
 * The same rules are written in Python in
 * `backend/tests/unit/docs/test_homepage_quotes_docs.py`, because a pull
 * request that changes only docs/ (or mkdocs.yml) runs no Jest. The two are
 * held together by behaviour, not by text: both run every case of
 * `tests/fixtures/home-quotes-cases.json` and must give its refusal, and the
 * fixture must have a case for every code in `RULES`. Change a rule in both
 * files and add its case, or the reader you did not change fails.
 *
 * A refused table gives every problem it can name; a row with a broken shape
 * is not read further, and a rule that needs something already refused (a
 * page nobody could read, a phrase of the capability page, its list item) is
 * not applied, so one defect gives one refusal.
 */

export interface Problem {
  code: (typeof RULES)[number];
  where: string;
  message: string;
}

/** Reads a file by its path from the repository root; null when it does not exist. */
export type Read = (rel: string) => string | null;

export type Part = string | { link: string; page: string };

/** A row as the page renders it. The table's own shape is checked by `problems()`. */
export interface QuoteRow {
  id: string;
  parts?: Part[];
  code?: string[];
}

/** Every refusal, by code. The fixture has a case for each. */
export const RULES = [
  'keys', // a key missing, or one the contract does not name
  'type', // a value of the wrong type
  'empty', // an empty list or an empty string
  'id', // an id that is not lower-case words joined by hyphens
  'duplicate-id',
  'page-name', // not a path under docs/ without `.md`
  'no-source', // a quote that cites no page
  'links', // more than one link in a quote
  'short-phrase', // a phrase under MIN_PHRASE characters
  'not-in-quote', // a phrase the quote does not contain
  'no-page', // a cited or linked page that does not exist: an error, never a skip
  'not-on-page', // a phrase the cited page does not contain
  'no-capability', // no phrase cited from the capability page, or no one list item of it holds them all
  'two-capabilities', // several list items of the capability page hold every phrase cited from it
  'not-in-nav', // a linked page the mkdocs nav does not list
  'no-title', // a linked page whose first line is not `# <title>`
  'link-text', // a link whose text is not its page's title, " (beta)" removed
  'says-beta', // the quote says "beta", and nothing it rests on, links or names is beta
  'omits-beta', // the quote rests on, links or names a beta, and does not say "beta"
  'code-not-on-page', // a code line that is not, once stripped, a line of its page
] as const;

/** A phrase this short proves little: "production data" is on many pages. */
export const MIN_PHRASE = 20;

const ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const PAGE = /^[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*$/;
const TITLE = /^# (.+)$/;
const BETA_TITLE = /^(.+) \(beta\)$/;
const BETA_WORD = /\bbeta\b/;
/** A prior's parameters, named by an item of the capability page: not a release stage. */
const PRIOR = /prior alpha and beta/g;
/** The capability page, as a source names it. */
const CAPABILITIES = 'capabilities';
/** An item marked beta right after its link; the group is the link's text, its name. */
const MARKED_ITEM = /^- \[([^\]]+)\]\([^)]*\) \([^)]*\bbeta\b[^)]*\)/i;

/** Both sides compared as prose: code marks dropped, whitespace collapsed, case folded. */
export const prose = (s: string): string => s.replace(/`/g, '').replace(/\s+/g, ' ').toLowerCase();

/** Whether a quote, or an item, says "beta". */
const saysBeta = (s: string): boolean => BETA_WORD.test(prose(s).replace(PRIOR, ''));

/** The capability page's list items: its lines that start "- ". */
const listItems = (page: string): string[] => page.split(/\r?\n/).filter((line) => line.startsWith('- '));

/** The names of the items marked beta right after their link. */
const betaNames = (items: string[]): string[] =>
  items.flatMap((item) => {
    const marked = MARKED_ITEM.exec(item);
    return marked === null ? [] : [marked[1]];
  });

/** mkdocs.yml lists `page` in its nav as `- <label>: <page>.md`. */
export const navEntry = (page: string): RegExp =>
  new RegExp(`^\\s+- [^:\\n]+: ${page.replace(/[.*+?^${}()|[\]\\/-]/g, '\\$&')}\\.md\\s*$`, 'm');

const firstLine = (text: string): string => text.split(/\r?\n/)[0];

type Obj = Record<string, unknown>;
const isObject = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const sameKeys = (o: Obj, keys: string[]): boolean =>
  Object.keys(o).length === keys.length && keys.every((k) => Object.prototype.hasOwnProperty.call(o, k));

/** Every problem with `table`, reading the cited pages and mkdocs.yml through `read`. */
export function problems(table: unknown, read: Read): Problem[] {
  const out: Problem[] = [];
  const add = (code: Problem['code'], where: string, message: string) => out.push({ code, where, message });

  if (!isObject(table) || !sameKeys(table, ['quotes'])) {
    add('keys', 'the table', 'the table is {"quotes": [...]} and nothing else');
    return out;
  }
  const quotes = table.quotes;
  if (!Array.isArray(quotes)) {
    add('type', 'quotes', 'quotes is a list');
    return out;
  }
  if (quotes.length === 0) {
    add('empty', 'quotes', 'the table has no quotes');
    return out;
  }

  const seen = new Set<string>();
  quotes.forEach((row: unknown, i: number) => {
    const where = `quotes[${i}]`;
    if (!isObject(row)) {
      add('type', where, 'a quote is an object');
      return;
    }
    const isCode = Object.prototype.hasOwnProperty.call(row, 'code');
    const keys = isCode ? ['id', 'code', 'sources'] : ['id', 'parts', 'sources'];
    if (!sameKeys(row, keys)) {
      add('keys', where, `a quote has exactly the keys ${keys.join(', ')}`);
      return;
    }
    const id = row.id;
    if (typeof id !== 'string' || !ID.test(id)) {
      add('id', where, `the id ${JSON.stringify(id)} is not lower-case words joined by hyphens`);
      return;
    }
    if (seen.has(id)) {
      add('duplicate-id', id, `two quotes have the id ${id}`);
      return;
    }
    seen.add(id);
    if (isCode) codeRow(id, row, read, add);
    else sentenceRow(id, row, read, add);
  });
  return out;
}

type Add = (code: Problem['code'], where: string, message: string) => void;

function sentenceRow(id: string, row: Obj, read: Read, add: Add): void {
  const parts = row.parts;
  if (!Array.isArray(parts)) return add('type', id, 'parts is a list');
  if (parts.length === 0) return add('empty', id, 'the quote has no parts');
  let text = '';
  const links: Array<{ link: string; page: string }> = [];
  for (const part of parts as unknown[]) {
    if (typeof part === 'string') {
      if (part === '') return add('empty', id, 'a part is an empty string');
      text += part;
    } else if (isObject(part)) {
      if (!sameKeys(part, ['link', 'page'])) return add('keys', id, 'a link is {"link", "page"} and nothing else');
      if (typeof part.link !== 'string' || typeof part.page !== 'string') {
        return add('type', id, "a link's text and page are strings");
      }
      if (part.link === '') return add('empty', id, 'a link has no text');
      if (!PAGE.test(part.page)) return add('page-name', id, `${JSON.stringify(part.page)} is not a page under docs/`);
      links.push({ link: part.link, page: part.page });
      text += part.link;
    } else {
      return add('type', id, 'a part is a string or a link');
    }
  }
  if (links.length > 1) return add('links', id, `${links.length} links in one quote; a quote has at most one`);

  const sources = row.sources;
  if (!Array.isArray(sources)) return add('type', id, 'sources is a list');
  if (sources.length === 0) return add('no-source', id, 'the quote cites no page');
  for (const source of sources as unknown[]) {
    if (!isObject(source) || !sameKeys(source, ['page', 'phrases'])) {
      return add('keys', id, 'a source is {"page", "phrases"} and nothing else');
    }
    if (typeof source.page !== 'string' || !Array.isArray(source.phrases)) {
      return add('type', id, "a source's page is a string and its phrases a list");
    }
    if (!PAGE.test(source.page)) return add('page-name', id, `${JSON.stringify(source.page)} is not a page under docs/`);
    if (source.phrases.length === 0) return add('empty', id, `docs/${source.page}.md is cited for no phrase`);
    if (!source.phrases.every((p: unknown) => typeof p === 'string')) return add('type', id, 'a phrase is a string');
  }

  const said = prose(text);
  // The phrases cited from the capability page; null once one of them is refused.
  let cited: string[] | null = [];
  for (const source of sources as Array<{ page: string; phrases: string[] }>) {
    const page = read(`docs/${source.page}.md`);
    if (page === null) add('no-page', id, `docs/${source.page}.md does not exist`);
    const onPage = page === null ? '' : prose(page);
    const held: string[] = []; // the phrases both the quote and the page hold
    for (const phrase of source.phrases) {
      const p = prose(phrase);
      if (p.trim().length < MIN_PHRASE) {
        add('short-phrase', id, `${JSON.stringify(phrase)} is under ${MIN_PHRASE} characters`);
        continue;
      }
      if (!said.includes(p)) add('not-in-quote', id, `${JSON.stringify(phrase)} is not in the quote`);
      if (page !== null && !onPage.includes(p)) {
        add('not-on-page', id, `${JSON.stringify(phrase)} is not on docs/${source.page}.md`);
      }
      if (said.includes(p) && onPage.includes(p)) held.push(p);
    }
    if (source.page === CAPABILITIES && cited !== null) {
      cited = held.length === source.phrases.length ? cited.concat(held) : null;
    }
  }

  const items = listItems(read(`docs/${CAPABILITIES}.md`) ?? '');
  const item = cited === null ? null : itemHolding(id, cited, items, add);

  let titledBeta = false;
  let titlesRead = true;
  for (const link of links) {
    const page = read(`docs/${link.page}.md`);
    if (page === null) {
      add('no-page', id, `the link's page docs/${link.page}.md does not exist`);
      titlesRead = false;
      continue;
    }
    const nav = read('mkdocs.yml');
    if (nav === null || !navEntry(link.page).test(nav)) {
      add('not-in-nav', id, `mkdocs.yml's nav does not list ${link.page}.md`);
    }
    const title = TITLE.exec(firstLine(page));
    if (title === null) {
      add('no-title', id, `docs/${link.page}.md does not open with "# <title>"`);
      titlesRead = false;
      continue;
    }
    const beta = BETA_TITLE.exec(title[1]);
    if (beta !== null) titledBeta = true;
    const name = beta !== null ? beta[1] : title[1];
    if (prose(link.link) !== prose(name)) {
      add('link-text', id, `the link reads ${JSON.stringify(link.link)}; docs/${link.page}.md is titled ${JSON.stringify(title[1])}`);
    }
  }
  if (item === null || !titlesRead) return;
  const why: string[] = [];
  if (saysBeta(item)) why.push(`the list item of docs/${CAPABILITIES}.md it rests on says "beta"`);
  if (titledBeta) why.push('the page it links is titled "(beta)"');
  for (const name of betaNames(items)) {
    if (said.includes(prose(name))) why.push(`it names ${JSON.stringify(name)}, which docs/${CAPABILITIES}.md marks beta`);
  }
  if (saysBeta(text) && why.length === 0) {
    add('says-beta', id, `the quote says "beta", and no list item of docs/${CAPABILITIES}.md it rests on or names, and no page it links, is marked beta`);
  }
  if (!saysBeta(text) && why.length > 0) add('omits-beta', id, `the quote does not say "beta": ${why.join('; ')}`);
}

/** The one list item that holds every phrase cited from the capability page; null, with a refusal, when none or several do. */
function itemHolding(id: string, cited: string[], items: string[], add: Add): string | null {
  // First: every() of an empty list is true, so with no phrase every item would hold them all.
  if (cited.length === 0) {
    add('no-capability', id, `the quote cites no phrase of docs/${CAPABILITIES}.md`);
    return null;
  }
  const hits = items.filter((item) => cited.every((phrase) => prose(item).includes(phrase)));
  if (hits.length === 1) return hits[0];
  if (hits.length === 0) {
    add('no-capability', id, `no one list item of docs/${CAPABILITIES}.md holds every phrase cited from it`);
  } else {
    add('two-capabilities', id, `${hits.length} list items of docs/${CAPABILITIES}.md hold every phrase cited from it; cite words only one has`);
  }
  return null;
}

function codeRow(id: string, row: Obj, read: Read, add: Add): void {
  const code = row.code;
  if (!Array.isArray(code)) return add('type', id, 'code is a list');
  if (code.length === 0) return add('empty', id, 'the code block has no lines');
  for (const line of code as unknown[]) {
    if (typeof line !== 'string') return add('type', id, 'a code line is a string');
    if (line.trim() === '') return add('empty', id, 'a code line is blank');
  }
  const sources = row.sources;
  if (!Array.isArray(sources)) return add('type', id, 'sources is a list');
  if (sources.length === 0) return add('no-source', id, 'the code block cites no page');
  for (const source of sources as unknown[]) {
    if (!isObject(source) || !sameKeys(source, ['page'])) return add('keys', id, 'a code source is {"page"} and nothing else');
    if (typeof source.page !== 'string') return add('type', id, "a source's page is a string");
    if (!PAGE.test(source.page)) return add('page-name', id, `${JSON.stringify(source.page)} is not a page under docs/`);
  }
  const lines = new Set<string>();
  let missing = false;
  for (const source of sources as Array<{ page: string }>) {
    const page = read(`docs/${source.page}.md`);
    if (page === null) {
      add('no-page', id, `docs/${source.page}.md does not exist`);
      missing = true;
      continue;
    }
    for (const line of page.split(/\r?\n/)) lines.add(line.trim());
  }
  if (missing) return;
  for (const line of code as string[]) {
    if (!lines.has(line.trim())) add('code-not-on-page', id, `${JSON.stringify(line)} is not a line of the page it cites`);
  }
}
