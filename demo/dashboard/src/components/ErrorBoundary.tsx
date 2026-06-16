import { Component, ReactNode } from 'react';
import { AlertTriangle, RotateCcw } from 'lucide-react';

interface Props { children: ReactNode }
interface State { error: Error | null }

// Catches render-time exceptions in the subtree and shows a styled fallback
// instead of unmounting React into a blank white page. Common cause here: a
// frontend bundle newer than the backend it's talking to (a field the UI reads
// is missing from an older API response).
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error) {
    // Surfaced for anyone with the console open; the fallback UI carries the gist.
    console.error('RunbookExecutor render error:', error);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="card animate-fade-in border-bad/40">
        <div className="card-body flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-bad" />
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-bad">This page hit a rendering error</h3>
            <p className="mt-1 text-xs text-ink-600 dark:text-ink-400">
              Often this means the dashboard is newer than the running server. Restart the demo
              server (<span className="font-mono">.\stop.ps1</span> then <span className="font-mono">.\start.ps1</span>)
              so its API matches this build, then reload.
            </p>
            <p className="mt-1 break-words font-mono text-[11px] text-ink-500 dark:text-ink-500">
              {this.state.error.message}
            </p>
            <button onClick={() => this.setState({ error: null })} className="btn mt-3 !py-1 !text-xs">
              <RotateCcw className="h-3.5 w-3.5" /> Retry
            </button>
          </div>
        </div>
      </div>
    );
  }
}
