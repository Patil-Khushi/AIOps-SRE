import { useLocation } from 'react-router-dom';
import { getAgentById, type AgentCatalogItem } from '@/data/agentCatalog';
import { getConsoleAgentId } from '@/lib/consoleScope';

// Resolve the agent the console is currently scoped to.
//
// On a per-agent live surface (`/agents/<id>`) that route IS the open agent —
// derive it from the path so the nav title + scoped sidebar are always correct
// (deterministic), regardless of what consoleScope last held. Anywhere else,
// fall back to the console scope the launcher set in localStorage.
//
// Shared by Header + Sidebar so the derivation lives in exactly one place
// (if the route structure changes, only this hook updates).
export function useConsoleAgent(): AgentCatalogItem | undefined {
  const { pathname } = useLocation();
  const routeAgentId =
    pathname.startsWith('/agents/') && pathname.split('/').length > 2
      ? pathname.split('/')[2]
      : null;
  return getAgentById(routeAgentId ?? getConsoleAgentId());
}
