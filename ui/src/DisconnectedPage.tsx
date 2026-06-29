interface DisconnectedPageProps {
  onRetry: () => void;
  checking: boolean;
}

export default function DisconnectedPage({
  onRetry,
  checking,
}: DisconnectedPageProps) {
  return (
    <div className="disconnected">
      <header className="disconnected-header">
        <div className="brand">
          <span className="mark">✦</span> Mileage
        </div>
      </header>
      <main className="disconnected-main">
        <h1 className="wordmark">Mileage</h1>
        <p className="disconnected-title">Server disconnected</p>
        <p className="disconnected-body">
          The API or dev server is not running. Start both processes again, then
          retry.
        </p>
        <div className="disconnected-steps">
          <p>
            <strong>Terminal 1 — API</strong>
            <code>uvicorn mileage.api.app:app --reload --port 8000</code>
          </p>
          <p>
            <strong>Terminal 2 — UI</strong>
            <code>cd ui && npm run dev</code>
          </p>
        </div>
        <button
          type="button"
          className="cta disconnected-retry"
          onClick={onRetry}
          disabled={checking}
        >
          {checking ? "Checking…" : "Retry connection"}
        </button>
      </main>
    </div>
  );
}
