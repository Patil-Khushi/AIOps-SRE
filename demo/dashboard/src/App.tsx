import { Routes, Route, Navigate } from 'react-router-dom';
import Layout from '@/components/Layout';
import BrowseLayout from '@/components/BrowseLayout';
import Landing from '@/pages/Landing';
import Overview from '@/pages/Overview';
import Agents from '@/pages/Agents';
import AgentDetail from '@/pages/AgentDetail';
import AlertStream from '@/pages/AlertStream';
import Notifications from '@/pages/Notifications';
import Reasoning from '@/pages/Reasoning';
import Topology from '@/pages/Topology';
import SystemHealth from '@/pages/SystemHealth';

export default function App() {
  return (
    <Routes>
      {/* Immersive portal landing — boot-curtain → hero reveal. */}
      <Route path="/" element={<Landing />} />

      {/* Agent browser — phase → agent drill-down, no console sidebar. */}
      <Route element={<BrowseLayout />}>
        <Route path="/agents" element={<Agents />} />
        <Route path="/agents/:agentId" element={<AgentDetail />} />
      </Route>

      {/* Operations console — existing dashboard, now nested under /console. */}
      <Route path="/console" element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="alerts" element={<AlertStream />} />
        <Route path="notifications" element={<Notifications />} />
        <Route path="reasoning" element={<Reasoning />} />
        <Route path="topology" element={<Topology />} />
        <Route path="health" element={<SystemHealth />} />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
