import { Routes, Route, Navigate } from 'react-router-dom';
import Layout from '@/components/Layout';
import BrowseLayout from '@/components/BrowseLayout';
import Landing from '@/pages/Landing';
import Overview from '@/pages/Overview';
import Agents from '@/pages/Agents';
import AgentDetail from '@/pages/AgentDetail';
import SreOps from '@/pages/SreOps';
import Integrations from '@/pages/Integrations';
import AlertStream from '@/pages/AlertStream';
import Approvals from '@/pages/Approvals';
import NotificationAssembler from '@/pages/NotificationAssembler';
import Reasoning from '@/pages/Reasoning';
import RcaConsole from '@/pages/RcaConsole';
import IncidentCommander from '@/pages/IncidentCommander';
import Topology from '@/pages/Topology';
import SystemHealth from '@/pages/SystemHealth';
import Knowledge from '@/pages/Knowledge';
import RunbookExecutor from '@/pages/RunbookExecutor';
import AutoHealer from '@/pages/AutoHealer';
import ErrorBoundary from '@/components/ErrorBoundary';

export default function App() {
  return (
    <Routes>
      {/* Immersive portal landing — boot-curtain → hero reveal. The boot
          animation plays only on the first visit; afterwards the landing shows
          instantly (see Landing/usePortalProgress), so home is always here. */}
      <Route path="/" element={<Landing />} />

      {/* Agent browser — phase → agent drill-down, no console sidebar. */}
      <Route element={<BrowseLayout />}>
        <Route path="/agents" element={<Agents />} />
        <Route path="/agents/:agentId" element={<AgentDetail />} />
        <Route path="/sre-ops" element={<SreOps />} />
        <Route path="/integrations" element={<Integrations />} />
      </Route>

      {/* Operations console — existing dashboard, now nested under /console. */}
      <Route path="/console" element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="alerts" element={<AlertStream />} />
        <Route path="rca" element={<RcaConsole />} />
        <Route path="incident-commander" element={<IncidentCommander />} />
        <Route path="approvals" element={<Approvals />} />
        <Route path="notifications" element={<NotificationAssembler />} />
        {/* Legacy war-room path → folded into the combined notifications surface. */}
        <Route path="war-room" element={<Navigate to="/console/notifications" replace />} />
        <Route path="reasoning" element={<Reasoning />} />
        <Route path="knowledge" element={<Knowledge />} />
        <Route path="topology" element={<Topology />} />
        <Route path="health" element={<SystemHealth />} />
      </Route>

      {/* Per-agent live surfaces — share the console chrome. Linked from the
          agent catalog as /agents/<id> (e.g. /agents/runbook-executor). */}
      <Route path="/agents" element={<Layout />}>
        <Route
          path="runbook-executor"
          element={<ErrorBoundary><RunbookExecutor /></ErrorBoundary>}
        />
        {/* Remediation Recommender is merged into the RCA console — its
            human-selectable fix steps + approval now live there. The standalone
            page is retired; any old link redirects to the combined surface. */}
        <Route
          path="remediation-recommender"
          element={<Navigate to="/console/rca" replace />}
        />
        <Route
          path="auto-healer"
          element={<ErrorBoundary><AutoHealer /></ErrorBoundary>}
        />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
