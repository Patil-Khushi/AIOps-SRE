import { makeCache } from './persistentCache';

// One run_id per incident-list row, persisted so re-opening the chat for the
// same incident resumes the same run/conversation instead of starting fresh.
const cache = makeCache<string>('icc-run-id');

export function runIdFor(key: string): string {
  const existing = cache.get(key);
  if (existing) return existing;
  const id = crypto.randomUUID();
  cache.set(key, id);
  return id;
}
