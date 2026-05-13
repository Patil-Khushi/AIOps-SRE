import { Routes, Route, Navigate } from 'react-router-dom';
import Layout from '@/components/Layout';
import Overview from '@/pages/Overview';
import AlertStream from '@/pages/AlertStream';
import Notifications from '@/pages/Notifications';
import Reasoning from '@/pages/Reasoning';
import Topology from '@/pages/Topology';
import SystemHealth from '@/pages/SystemHealth';

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="alerts" element={<AlertStream />} />
        <Route path="notifications" element={<Notifications />} />
        <Route path="reasoning" element={<Reasoning />} />
        <Route path="topology" element={<Topology />} />
        <Route path="health" element={<SystemHealth />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
