import { useEffect } from 'react';
import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import Header from './Header';
import { markEntered } from '@/lib/consoleScope';
import { ChatDockProvider } from '@/components/chat/ChatDockProvider';
import { RcaChatDock } from '@/components/chat/RcaChatDock';
import { ChatLauncherButton } from '@/components/chat/ChatLauncherButton';
import { ToastProvider } from '@/hooks/useToast';

export default function Layout() {
  // Inside the operations console → mark the app as entered so the landing
  // animation never replays on a later "/" visit.
  useEffect(() => markEntered(), []);
  return (
    <ToastProvider>
      <ChatDockProvider>
        <div className="flex min-h-screen w-full">
          <Sidebar />
          <div className="flex min-w-0 flex-1 flex-col">
            <Header />
            <main className="flex-1 overflow-y-auto p-6">
              <div className="mx-auto max-w-7xl animate-fade-in">
                <Outlet />
              </div>
            </main>
          </div>
          {/* Both fixed-position overlays, out of the flex flow entirely —
              opening/closing the chat never reflows the page beside them. */}
          <RcaChatDock />
          <ChatLauncherButton />
        </div>
      </ChatDockProvider>
    </ToastProvider>
  );
}
