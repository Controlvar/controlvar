import { docsUrl } from '@/services/docs';

/**
 * Every SDK in `sdk/`, by directory, with the name the public pages call it.
 *
 * The homepage and the docs hub used to state how many SDKs there were, and
 * the number drifted from `sdk/`, with one SDK missing from the list. So the
 * pages carry no count, and `tests/content/sdks.test.tsx` holds this map to
 * two facts:
 *
 *   1. its keys are exactly the directories under `sdk/`;
 *   2. every display name appears on the rendered homepage and docs hub.
 *
 * Adding an SDK directory therefore fails that test until it is named here and
 * on both pages. The two OpenFeature providers share one display name because
 * both pages describe them together.
 */
export const SDKS: Readonly<Record<string, string>> = {
  android: 'Android',
  dotnet: '.NET',
  edge: 'Edge',
  elixir: 'Elixir',
  flutter: 'Flutter',
  go: 'Go',
  ios: 'iOS',
  java: 'Java',
  js: 'JavaScript',
  openfeature: 'OpenFeature',
  'openfeature-python': 'OpenFeature',
  php: 'PHP',
  python: 'Python',
  react: 'React',
  'react-native': 'React Native',
  ruby: 'Ruby',
};

/**
 * Each SDK's guide, by the same keys as `SDKS`, for a page that links every
 * SDK from that list.
 *
 * Every value is a `docsUrl` call with a literal page, never one built from the key:
 * `docs-links.test.ts` checks a link's page only when the call is a literal,
 * and refuses any other call (#1459, PE 6). Two directories have a guide of
 * another name: `js` is sdk/javascript, and both OpenFeature providers share
 * sdk/openfeature. `tests/content/sdks.test.tsx` holds the keys to `SDKS`.
 */
export const SDK_GUIDES: Readonly<Record<string, string>> = {
  android: docsUrl('sdk/android'),
  dotnet: docsUrl('sdk/dotnet'),
  edge: docsUrl('sdk/edge'),
  elixir: docsUrl('sdk/elixir'),
  flutter: docsUrl('sdk/flutter'),
  go: docsUrl('sdk/go'),
  ios: docsUrl('sdk/ios'),
  java: docsUrl('sdk/java'),
  js: docsUrl('sdk/javascript'),
  openfeature: docsUrl('sdk/openfeature'),
  'openfeature-python': docsUrl('sdk/openfeature'),
  php: docsUrl('sdk/php'),
  python: docsUrl('sdk/python'),
  react: docsUrl('sdk/react'),
  'react-native': docsUrl('sdk/react-native'),
  ruby: docsUrl('sdk/ruby'),
};
