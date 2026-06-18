const LABELS = {
  regular: "Open",
  pre_market: "Pre-market",
  after_hours: "After-hours",
  closed: "Closed",
};

export default function MarketStatus({ status }) {
  if (!status) return <span className="muted">Market status…</span>;
  const cls = status.is_open ? "open" : status.session === "closed" ? "closed" : "ext";
  return (
    <div className={`market-status ${cls}`}>
      <span className="dot" />
      <span className="ms-label">Market {LABELS[status.session] || status.session}</span>
      <span className="muted small">
        {status.regular_open_et}–{status.regular_close_et} ET
      </span>
    </div>
  );
}
