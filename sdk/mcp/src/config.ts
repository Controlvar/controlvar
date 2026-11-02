/**
 * The server's configuration: three environment variables, read once at
 * start-up and checked for syntax only. No network call is made here.
 *
 * The checks run in a fixed order and the first failure is the one reported:
 * one line for stderr, exit code 2, and `initialize` is never answered.
 *
 *   0. no command-line argument at all (an argument may be a key);
 *   1. CONTROLVAR_API_URL is set, parses, and is http:// or https://;
 *   2. it has no user name, password, query or fragment;
 *   3. it is an origin only: no path beyond one trailing `/`;
 *   4. CONTROLVAR_ALLOW_INSECURE_HTTP, when present, is exactly `1`;
 *   5. plain http:// goes only to this computer (localhost, 127.0.0.1 or
 *      [::1], with no environment proxy in the way), unless that opt-in is
 *      set, in which case a warning is printed at every start;
 *   6. CONTROLVAR_API_KEY is set and looks like an API key.
 *
 * Requests are built from the parsed URL's `origin`, never from the raw
 * string, so the value checked here is the value used.
 */

export const ENV_URL = "CONTROLVAR_API_URL";
export const ENV_KEY = "CONTROLVAR_API_KEY";
export const ENV_INSECURE = "CONTROLVAR_ALLOW_INSECURE_HTTP";

/** Every environment variable the server reads, in the order it reads them. */
export const ENV_NAMES = [ENV_URL, ENV_KEY, ENV_INSECURE] as const;

/** "This computer": an exact match on the parsed URL's `hostname`. */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

const MAX_KEY_LENGTH = 200;
const MAX_ECHO_LENGTH = 200;

export interface Config {
  /** The instance's origin, e.g. `https://controlvar.example.com`. */
  origin: string;
  apiKey: string;
  /** Lines to print on stderr before the server starts (W1). */
  warnings: string[];
}

export type ConfigResult =
  | { ok: true; config: Config }
  | { ok: false; message: string };

export const ARGUMENTS_REFUSED =
  "This server takes no arguments; your MCP client starts it with none. " +
  "Put its settings in the environment: CONTROLVAR_API_URL and CONTROLVAR_API_KEY.";

type Env = Record<string, string | undefined>;

function refuse(message: string): ConfigResult {
  return { ok: false, message };
}

/**
 * Whether a refused URL value may be printed: only a short, printable value
 * with no `@`, `?` or `#`, so a user name, password, query or fragment (or a
 * key pasted into the wrong variable with one of them) is never echoed.
 */
function echoable(value: string): boolean {
  return (
    value.length <= MAX_ECHO_LENGTH &&
    /^[\x20-\x7E]*$/.test(value) &&
    !/[@?#]/.test(value)
  );
}

/**
 * Whether Node sends this process's fetch through an environment proxy.
 * `NODE_OPTIONS` is read as well as `execArgv`: a Node that accepts
 * `--use-env-proxy` there would not show it in `execArgv`.
 */
function usesEnvironmentProxy(env: Env, execArgv: readonly string[]): boolean {
  if ((env.NODE_USE_ENV_PROXY ?? "") !== "") return true;
  if (execArgv.some((arg) => arg === "--use-env-proxy" || arg.startsWith("--use-env-proxy="))) {
    return true;
  }
  return /(^|\s)--use-env-proxy(=|\s|$)/.test(env.NODE_OPTIONS ?? "");
}

export function readConfig(
  args: readonly string[],
  env: Env,
  execArgv: readonly string[],
): ConfigResult {
  // 0. Arguments: none. The argument itself is never echoed.
  if (args.length > 0) return refuse(ARGUMENTS_REFUSED);

  // 1. The address: present, parseable, http or https.
  const raw = env[ENV_URL];
  if (raw === undefined || raw.trim() === "") {
    return refuse(
      `${ENV_URL} is not set. Set it to your instance's address, for example http://localhost:8000.`,
    );
  }
  const notHttp = echoable(raw)
    ? `${ENV_URL} is "${raw}", which is not an http:// or https:// address.`
    : `${ENV_URL} is not an http:// or https:// address.`;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return refuse(`${notHttp} Set it to your instance's address, for example http://localhost:8000.`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return refuse(`${notHttp} Set it to your instance's address, for example http://localhost:8000.`);
  }

  // 2. No user info, query or fragment, judged on the parsed fields. The
  // value is not echoed.
  if (url.username !== "" || url.password !== "" || url.search !== "" || url.hash !== "") {
    return refuse(
      `${ENV_URL} contains a user name, password, query or fragment, which this server does not accept. ` +
        "Set it to your instance's address only, for example https://controlvar.example.com.",
    );
  }

  // 3. An origin only: a scheme, a host and an optional port.
  if (url.pathname !== "/" && url.pathname !== "") {
    const shown = echoable(raw) ? `is "${raw}", which has a path` : "has a path";
    return refuse(
      `${ENV_URL} ${shown}. Set it to the address only, without a path: ` +
        "for example https://controlvar.example.com.",
    );
  }

  // 4. The opt-in: absent, or exactly "1". Its value is never echoed.
  const hasOptIn = Object.prototype.hasOwnProperty.call(env, ENV_INSECURE) && env[ENV_INSECURE] !== undefined;
  if (hasOptIn && env[ENV_INSECURE] !== "1") {
    return refuse(
      `${ENV_INSECURE} has a value this server does not accept. ` +
        "Remove it, or set it to 1 to allow a plain http:// address on another computer.",
    );
  }
  const optedIn = hasOptIn;

  // 5. Plain HTTP only to this computer, unless opted in.
  const warnings: string[] = [];
  if (url.protocol === "http:") {
    const proxied = usesEnvironmentProxy(env, execArgv);
    const localName = LOCAL_HOSTS.has(url.hostname);
    if (!localName || proxied) {
      const throughProxy = localName && proxied;
      if (!optedIn) {
        if (throughProxy) {
          return refuse(
            `${ENV_URL} is "${url.origin}", a plain http:// address, and NODE_USE_ENV_PROXY ` +
              "(or --use-env-proxy) makes Node send requests through a proxy, so your API key would " +
              "cross the network unencrypted. Set NODE_USE_ENV_PROXY to an empty value in this " +
              "server's settings, or use your instance's https:// address.",
          );
        }
        return refuse(
          `${ENV_URL} is "${url.origin}", a plain http:// address on another computer, so your API key ` +
            "would cross the network unencrypted. Use your instance's https:// address. If it runs on " +
            `a private network you trust and has no HTTPS, set ${ENV_INSECURE}=1 in this server's settings.`,
        );
      }
      if (throughProxy) {
        warnings.push(
          `Warning: ${ENV_INSECURE}=1 and NODE_USE_ENV_PROXY (or --use-env-proxy) is set: this server ` +
            `sends your API key and the answers over plain HTTP to ${url.host} through a proxy, where ` +
            "anyone who can watch that network can read them. Set NODE_USE_ENV_PROXY to an empty value " +
            "in this server's settings.",
        );
      } else {
        warnings.push(
          `Warning: ${ENV_INSECURE}=1: this server sends your API key and the answers over plain HTTP ` +
            `to ${url.host}, where anyone who can watch that network can read them. Use https:// when ` +
            "your instance offers it.",
        );
      }
    }
  }

  // 6. The key. Never echoed.
  const key = (env[ENV_KEY] ?? "").trim();
  if (key === "") {
    return refuse(
      `${ENV_KEY} is not set. Create an API key and set ${ENV_KEY} to it in your MCP client's ` +
        "settings for this server.",
    );
  }
  if (key.startsWith("eyJ")) {
    return refuse(
      `${ENV_KEY} looks like a sign-in token, not an API key. Create an API key and set ` +
        `${ENV_KEY} to it.`,
    );
  }
  if (key.length > MAX_KEY_LENGTH || !/^[\x21-\x7E]+$/.test(key)) {
    return refuse(
      `${ENV_KEY} is not an API key: a key is at most ${MAX_KEY_LENGTH} printable characters, ` +
        "with no spaces.",
    );
  }

  return { ok: true, config: { origin: url.origin, apiKey: key, warnings } };
}
