import { createContext, useContext, useState, type ReactNode } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CartesianGrid, Area, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, download, newKey, session } from "./api";

const Toast = createContext<(m: string) => void>(() => {});
const useToast = () => useContext(Toast);
const errMsg = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong");

function Async({ q, children }: { q: { isLoading: boolean; error: unknown; data: any }; children: (d: any) => ReactNode }) {
  if (q.isLoading) return <p className="muted">Loading…</p>;
  if (q.error) return <p className="err">Could not load: {errMsg(q.error)}</p>;
  return <>{children(q.data)}</>;
}

/* ------------------------------------------------------------ auth + shell */
function AuthPage() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [f, setF] = useState({ email: "", password: "", full_name: "" });
  const [err, setErr] = useState("");
  const nav = useNavigate();
  const submit = async () => {
    setErr("");
    try {
      if (mode === "register") await api("/auth/register", { body: f });
      const t = await api("/auth/login", { body: { email: f.email, password: f.password } });
      session.token = t.access_token;
      const me = await api("/auth/me");
      session.org = me.organizations[0]?.id ?? null;
      nav(session.org ? "/" : "/onboarding");
    } catch (e) { setErr(errMsg(e)); }
  };
  return (
    <div className="auth"><h1>StockSense</h1><p className="muted">Intelligent Inventory. Smarter Decisions.</p>
      <div className="card">
        {mode === "register" && <input placeholder="Full name" value={f.full_name} onChange={e => setF({ ...f, full_name: e.target.value })} />}
        <input placeholder="Email" value={f.email} onChange={e => setF({ ...f, email: e.target.value })} />
        <input type="password" placeholder="Password (10+ characters)" value={f.password} onChange={e => setF({ ...f, password: e.target.value })} />
        {err && <div className="err">{err}</div>}
        <button onClick={submit}>{mode === "login" ? "Sign in" : "Create account"}</button>
        <button className="secondary" onClick={() => setMode(mode === "login" ? "register" : "login")}>
          {mode === "login" ? "New here? Register" : "Have an account? Sign in"}</button>
      </div></div>);
}

function Onboarding() {
  const [f, setF] = useState({ name: "", industry: "general_retail", currency: "INR", timezone: "Asia/Kolkata", branch_name: "Main Store", branch_code: "MAIN" });
  const [err, setErr] = useState("");
  const nav = useNavigate();
  const create = async () => {
    try { const o = await api("/organizations", { body: f }); session.org = o.id; nav("/"); } catch (e) { setErr(errMsg(e)); }
  };
  return (<div className="auth"><h2>Set up your business</h2><div className="card">
    <input placeholder="Business name" value={f.name} onChange={e => setF({ ...f, name: e.target.value })} />
    <select value={f.industry} onChange={e => setF({ ...f, industry: e.target.value })}>
      {["general_retail", "grocery", "fashion", "electronics", "cosmetics", "wholesale"].map(x => <option key={x}>{x}</option>)}</select>
    <input placeholder="Currency (e.g. INR)" value={f.currency} onChange={e => setF({ ...f, currency: e.target.value.toUpperCase() })} />
    <input placeholder="First branch name" value={f.branch_name} onChange={e => setF({ ...f, branch_name: e.target.value })} />
    <input placeholder="Branch code" value={f.branch_code} onChange={e => setF({ ...f, branch_code: e.target.value })} />
    {err && <div className="err">{err}</div>}<button disabled={!f.name} onClick={create}>Create workspace</button></div></div>);
}

function Shell({ children }: { children: ReactNode }) {
  const nav = useNavigate();
  const links: [string, string][] = [["/", "Overview"], ["/products", "Products & Inventory"], ["/sales", "Sales"],
    ["/purchasing", "Purchasing"], ["/forecasting", "Forecasting"], ["/replenishment", "Replenishment"], ["/health", "Inventory Health"]];
  return (<div className="shell"><nav><h1>StockSense</h1><small>Intelligent Inventory. Smarter Decisions.</small>
    {links.map(([to, label]) => <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>{label}</NavLink>)}
    <button className="secondary" style={{ marginTop: "auto" }} onClick={async () => { try { await api("/auth/logout", { method: "POST" }); } catch {} session.token = null; session.org = null; nav("/login"); }}>Sign out</button>
  </nav><main>{children}</main></div>);
}

function useBranch() {
  const q = useQuery({ queryKey: ["branches"], queryFn: () => api("/branches") });
  const [sel, setSel] = useState<string>("");
  const branches: any[] = q.data ?? [];
  return { branches, branch: sel || branches[0]?.id || "", setBranch: setSel, loading: q.isLoading };
}
function BranchPicker({ b }: { b: ReturnType<typeof useBranch> }) {
  return <select value={b.branch} onChange={e => b.setBranch(e.target.value)}>{b.branches.map(x => <option key={x.id} value={x.id}>{x.name}</option>)}</select>;
}
const fmt = (n: any) => (n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 }));

/* ---------------------------------------------------------------- overview */
function Overview() {
  const b = useBranch();
  const q = useQuery({ queryKey: ["overview", b.branch], queryFn: () => api(`/analytics/overview?branch_id=${b.branch}&days=30`), enabled: !!b.branch });
  return (<><h2>Overview</h2><div className="row"><BranchPicker b={b} /></div>
    <Async q={q}>{(d) => (<>
      <div className="grid">{[["Inventory value (at cost)", d.inventory_valuation_at_cost], ["Active variants", d.active_variants], ["Low stock", d.low_stock_count],
        ["Out of stock", d.out_of_stock_count], ["Dead-stock estimate (cost)", d.dead_stock_value_estimate], ["Sales revenue (30d)", d.sales_revenue]].map(([l, v]) =>
        <div className="card stat" key={String(l)}><b>{fmt(v)}</b><span>{l}</span></div>)}</div>
      <div className="card"><h3>Sales trend (30 days)</h3>
        {d.sales_trend.length === 0 ? <p className="muted">No completed sales in this period yet. Record a sale or import history.</p> :
          <ResponsiveContainer width="100%" height={260}><ComposedChart data={d.sales_trend}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="date" /><YAxis /><Tooltip />
            <Line dataKey="revenue" stroke="#4f46e5" dot={false} /></ComposedChart></ResponsiveContainer>}
        <p className="muted">{d.margin_note} Estimated gross margin: {fmt(d.estimated_gross_margin)}. Dead stock is an estimate based on no sales in {d.dead_stock_count} item(s)’ inactivity window.</p></div>
    </>)}</Async></>);
}

/* ---------------------------------------------------------------- products */
function Products() {
  const qc = useQueryClient(); const toast = useToast();
  const [search, setSearch] = useState(""); const [page, setPage] = useState(1);
  const [np, setNp] = useState({ sku: "", name: "", cost_price: "0", selling_price: "0" });
  const [csv, setCsv] = useState(""); const [preview, setPreview] = useState<any>(null);
  const q = useQuery({ queryKey: ["products", search, page], queryFn: () => api(`/products?q=${encodeURIComponent(search)}&page=${page}&page_size=15`) });
  const create = useMutation({ mutationFn: () => api("/products", { body: np }), onSuccess: () => { toast("Product created"); setNp({ sku: "", name: "", cost_price: "0", selling_price: "0" }); qc.invalidateQueries({ queryKey: ["products"] }); }, onError: e => toast(errMsg(e)) });
  const imp = async (commit: boolean) => { try { const r = await api("/products/import", { body: { csv, commit } }); setPreview(r); if (r.committed) { toast(`Imported ${r.valid_rows} products`); qc.invalidateQueries({ queryKey: ["products"] }); } } catch (e) { toast(errMsg(e)); } };
  return (<><h2>Products & Inventory</h2>
    <div className="card"><h3>Add product</h3><div className="row">
      <input placeholder="SKU" value={np.sku} onChange={e => setNp({ ...np, sku: e.target.value })} />
      <input placeholder="Name" value={np.name} onChange={e => setNp({ ...np, name: e.target.value })} />
      <input placeholder="Cost" value={np.cost_price} onChange={e => setNp({ ...np, cost_price: e.target.value })} />
      <input placeholder="Price" value={np.selling_price} onChange={e => setNp({ ...np, selling_price: e.target.value })} />
      <button disabled={!np.sku || !np.name || create.isPending} onClick={() => create.mutate()}>Create</button></div></div>
    <div className="card"><div className="row"><input placeholder="Search name or SKU" value={search} onChange={e => { setSearch(e.target.value); setPage(1); }} /></div>
      <Async q={q}>{(d) => d.items.length === 0 ? <p className="muted">No products yet. Add one above or import a CSV below.</p> : (<>
        <table><thead><tr><th>SKU</th><th>Name</th><th>Cost</th><th>Price</th><th>Variants</th></tr></thead><tbody>
          {d.items.map((p: any) => <tr key={p.id}><td>{p.sku}</td><td>{p.name}</td><td>{fmt(p.cost_price)}</td><td>{fmt(p.selling_price)}</td><td>{p.variants.length}</td></tr>)}</tbody></table>
        <div className="row" style={{ marginTop: 12 }}><button className="secondary" disabled={page === 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span className="muted">Page {page} · {d.total} total</span><button className="secondary" disabled={page * 15 >= d.total} onClick={() => setPage(page + 1)}>Next</button></div></>)}</Async></div>
    <div className="card"><h3>Import products (CSV)</h3><p className="muted">Columns: sku, name, cost_price, selling_price, category, unit, barcode. Nothing is saved unless every row is valid.</p>
      <textarea rows={5} style={{ width: "100%" }} value={csv} onChange={e => setCsv(e.target.value)} placeholder="sku,name,cost_price,selling_price&#10;A1,Alpha,5,9" />
      <div className="row"><button className="secondary" disabled={!csv} onClick={() => imp(false)}>Validate</button><button disabled={!csv} onClick={() => imp(true)}>Import</button></div>
      {preview && <div><p>{preview.valid_rows} valid · {preview.error_rows} with errors{preview.committed && " · committed"}</p>
        {preview.errors.map((e: any) => <div className="err" key={e.line}>Line {e.line} ({e.sku || "no sku"}): {e.errors.join("; ")}</div>)}</div>}</div>
    <StockPanel /></>);
}

function StockPanel() {
  const qc = useQueryClient(); const toast = useToast();
  const wh = useQuery({ queryKey: ["warehouses"], queryFn: () => api("/warehouses") });
  const prods = useQuery({ queryKey: ["products", "", 1, "all"], queryFn: () => api("/products?page_size=200") });
  const [f, setF] = useState({ warehouse_id: "", variant_id: "", quantity: "1", reason: "found", kind: "opening" });
  const bal = useQuery({ queryKey: ["balances"], queryFn: () => api("/inventory/balances?page_size=200") });
  const mv = useQuery({ queryKey: ["movements"], queryFn: () => api("/inventory/movements?page_size=15") });
  const variants = (prods.data?.items ?? []).flatMap((p: any) => p.variants.map((v: any) => ({ id: v.id, label: `${v.sku} — ${p.name}` })));
  const w = f.warehouse_id || wh.data?.[0]?.id || ""; const v = f.variant_id || variants[0]?.id || "";
  const run = useMutation({
    mutationFn: () => f.kind === "opening" ? api("/inventory/opening-balances", { body: { warehouse_id: w, variant_id: v, quantity: f.quantity }, headers: { "Idempotency-Key": newKey() } })
      : api("/inventory/adjustments", { body: { warehouse_id: w, variant_id: v, quantity: f.quantity, reason: f.reason }, headers: { "Idempotency-Key": newKey() } }),
    onSuccess: () => { toast("Stock updated"); qc.invalidateQueries({ queryKey: ["balances"] }); qc.invalidateQueries({ queryKey: ["movements"] }); }, onError: e => toast(errMsg(e)) });
  return (<div className="card"><h3>Stock</h3>
    <div className="row"><select value={w} onChange={e => setF({ ...f, warehouse_id: e.target.value })}>{(wh.data ?? []).map((x: any) => <option key={x.id} value={x.id}>{x.name}</option>)}</select>
      <select value={v} onChange={e => setF({ ...f, variant_id: e.target.value })}>{variants.map((x: any) => <option key={x.id} value={x.id}>{x.label}</option>)}</select>
      <select value={f.kind} onChange={e => setF({ ...f, kind: e.target.value })}><option value="opening">Opening balance</option><option value="adjust">Adjustment</option></select>
      {f.kind === "adjust" && <select value={f.reason} onChange={e => setF({ ...f, reason: e.target.value })}>{["found", "lost", "damaged", "expired"].map(r => <option key={r}>{r}</option>)}</select>}
      <input style={{ width: 90 }} value={f.quantity} onChange={e => setF({ ...f, quantity: e.target.value })} />
      <button disabled={!w || !v || run.isPending} onClick={() => run.mutate()}>Apply</button></div>
    <Async q={bal}>{(d) => d.items.length === 0 ? <p className="muted">No stock recorded yet. Enter an opening balance above.</p> :
      <table><thead><tr><th>SKU</th><th>Sellable</th><th>Unavailable</th><th>Reserved</th></tr></thead><tbody>{d.items.map((b: any) =>
        <tr key={b.warehouse_id + b.variant_id}><td>{b.sku}</td><td>{fmt(b.on_hand_quantity)}</td><td>{fmt(b.unavailable_quantity)}</td><td>{fmt(b.reserved_quantity)}</td></tr>)}</tbody></table>}</Async>
    <h4>Recent movements</h4><Async q={mv}>{(d) => <table><thead><tr><th>When</th><th>Type</th><th>Δ</th><th>Balance after</th><th>Reason</th></tr></thead><tbody>{d.items.map((m: any) =>
      <tr key={m.id}><td>{new Date(m.occurred_at).toLocaleString()}</td><td>{m.movement_type}</td><td>{m.quantity_delta}</td><td>{m.balance_after}</td><td>{m.reason}</td></tr>)}</tbody></table>}</Async></div>);
}

/* ------------------------------------------------------------------- sales */
function Sales() {
  const b = useBranch(); const qc = useQueryClient(); const toast = useToast();
  const prods = useQuery({ queryKey: ["products", "", 1, "all"], queryFn: () => api("/products?page_size=200") });
  const sales = useQuery({ queryKey: ["sales"], queryFn: () => api("/sales?page_size=20") });
  const variants = (prods.data?.items ?? []).flatMap((p: any) => p.variants.map((v: any) => ({ id: v.id, label: `${v.sku} — ${p.name}`, price: v.price })));
  const [line, setLine] = useState({ variant_id: "", quantity: "1" }); const [key, setKey] = useState(newKey());
  const vid = line.variant_id || variants[0]?.id || "";
  const rec = useMutation({ mutationFn: () => api("/sales", { body: { branch_id: b.branch, items: [{ variant_id: vid, quantity: line.quantity }] }, headers: { "Idempotency-Key": key } }),
    onSuccess: () => { toast("Sale recorded"); setKey(newKey()); qc.invalidateQueries({ queryKey: ["sales"] }); qc.invalidateQueries({ queryKey: ["balances"] }); }, onError: e => toast(errMsg(e)) });
  const cancel = useMutation({ mutationFn: (id: string) => api(`/sales/${id}/cancel`, { method: "POST" }), onSuccess: () => { toast("Sale cancelled and stock restored"); qc.invalidateQueries({ queryKey: ["sales"] }); }, onError: e => toast(errMsg(e)) });
  return (<><h2>Sales</h2><div className="card"><h3>Record a sale</h3><div className="row"><BranchPicker b={b} />
    <select value={vid} onChange={e => setLine({ ...line, variant_id: e.target.value })}>{variants.map((x: any) => <option key={x.id} value={x.id}>{x.label}</option>)}</select>
    <input style={{ width: 80 }} value={line.quantity} onChange={e => setLine({ ...line, quantity: e.target.value })} />
    <button disabled={!b.branch || !vid || rec.isPending} onClick={() => rec.mutate()}>Record sale</button></div></div>
    <div className="card"><h3>History</h3><Async q={sales}>{(d) => d.items.length === 0 ? <p className="muted">No sales yet.</p> :
      <table><thead><tr><th>#</th><th>Date</th><th>Status</th><th>Total</th><th /></tr></thead><tbody>{d.items.map((s: any) =>
        <tr key={s.id}><td>{s.sale_number}</td><td>{s.sale_date}</td><td>{s.status}</td><td>{fmt(s.grand_total)}</td>
          <td>{s.status === "completed" && <button className="secondary" onClick={() => window.confirm(`Cancel ${s.sale_number}? Stock will be restored.`) && cancel.mutate(s.id)}>Cancel</button>}</td></tr>)}</tbody></table>}</Async></div></>);
}

/* -------------------------------------------------------------- purchasing */
function Purchasing() {
  const qc = useQueryClient(); const toast = useToast();
  const q = useQuery({ queryKey: ["pos"], queryFn: () => api("/purchase-orders?page_size=50") });
  const act = useMutation({ mutationFn: ({ id, a }: { id: string; a: string }) => api(`/purchase-orders/${id}/${a}`, { method: "POST" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["pos"] }); toast("Updated"); }, onError: e => toast(errMsg(e)) });
  const recv = useMutation({ mutationFn: (p: any) => api("/goods-receipts", { body: { purchase_order_id: p.id, items: p.items.map((i: any) => {
      const rem = Number(i.ordered_quantity) - Number(i.received_quantity) - Number(i.rejected_quantity);
      return { purchase_order_item_id: i.id, received_quantity: String(rem), accepted_quantity: String(rem) }; }).filter((i: any) => Number(i.received_quantity) > 0) }, headers: { "Idempotency-Key": newKey() } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["pos"] }); qc.invalidateQueries({ queryKey: ["balances"] }); toast("Goods received into stock"); }, onError: e => toast(errMsg(e)) });
  return (<><h2>Purchasing</h2><p className="muted">Purchase orders are created from accepted replenishment recommendations or via the API. Nothing is ever sent to a supplier automatically.</p>
    <div className="card"><Async q={q}>{(d) => d.items.length === 0 ? <p className="muted">No purchase orders yet.</p> :
      <table><thead><tr><th>Order</th><th>Status</th><th>Total</th><th>Lines</th><th>Actions</th></tr></thead><tbody>{d.items.map((p: any) =>
        <tr key={p.id}><td>{p.order_number}</td><td>{p.status}</td><td>{fmt(p.total)}</td><td>{p.items.length}</td><td className="row">
          {p.status === "draft" && <button className="secondary" onClick={() => act.mutate({ id: p.id, a: "submit" })}>Submit</button>}
          {p.status === "pending_approval" && <button onClick={() => window.confirm("Approve this purchase order?") && act.mutate({ id: p.id, a: "approve" })}>Approve</button>}
          {["approved", "partially_received"].includes(p.status) && <button onClick={() => window.confirm("Receive all remaining quantities as accepted?") && recv.mutate(p)}>Receive remaining</button>}
          {["draft", "pending_approval", "approved"].includes(p.status) && <button className="secondary" onClick={() => window.confirm("Cancel this order?") && act.mutate({ id: p.id, a: "cancel" })}>Cancel</button>}</td></tr>)}</tbody></table>}</Async></div></>);
}

/* ------------------------------------------------------------- forecasting */
function Forecasting() {
  const b = useBranch(); const qc = useQueryClient(); const toast = useToast();
  const [horizon, setHorizon] = useState(30); const [runId, setRunId] = useState(""); const [variant, setVariant] = useState("");
  const runs = useQuery({ queryKey: ["fruns"], queryFn: () => api("/forecasts"), refetchInterval: 4000 });
  const start = useMutation({ mutationFn: () => api("/forecasts", { body: { branch_id: b.branch, horizon_days: horizon } }),
    onSuccess: r => { setRunId(r.id); toast(`Forecast ${r.status}`); qc.invalidateQueries({ queryKey: ["fruns"] }); }, onError: e => toast(errMsg(e)) });
  const run = (runs.data ?? []).find((r: any) => r.id === runId) ?? (runs.data ?? [])[0];
  const prods = useQuery({ queryKey: ["products", "", 1, "all"], queryFn: () => api("/products?page_size=200") });
  const variants = (prods.data?.items ?? []).flatMap((p: any) => p.variants.map((v: any) => ({ id: v.id, label: `${v.sku} — ${p.name}` })));
  const vid = variant || variants[0]?.id || "";
  const res = useQuery({ queryKey: ["fres", run?.id, vid], queryFn: () => api(`/forecasts/${run.id}/results?variant_id=${vid}`), enabled: !!run && run.status === "completed" && !!vid });
  const chart = res.data ? [...res.data.history.map((h: any) => ({ date: h.date, actual: h.actual })),
    ...res.data.results.map((r: any) => ({ date: r.forecast_period, predicted: +r.predicted_quantity, band: [+r.lower_bound, +r.upper_bound] }))] : [];
  const meta = res.data?.results?.[0]?.confidence_metadata;
  return (<><h2>Forecasting</h2><div className="card"><div className="row"><BranchPicker b={b} />
    <select value={horizon} onChange={e => setHorizon(+e.target.value)}>{[7, 30, 60, 90].map(h => <option key={h} value={h}>{h} days</option>)}</select>
    <button disabled={!b.branch || start.isPending} onClick={() => start.mutate()}>Generate forecast</button></div>
    <p className="muted">Forecasts come from statistical models chosen by time-series backtesting. They are estimates, not guarantees.</p></div>
    <div className="card"><h3>Runs</h3><table><thead><tr><th>Created</th><th>Horizon</th><th>Status</th><th>Models used</th><th /></tr></thead><tbody>{(runs.data ?? []).map((r: any) =>
      <tr key={r.id}><td>{new Date(r.created_at).toLocaleString()}</td><td>{r.horizon_days}d</td><td>{r.status}{r.error && <span className="err"> — {r.error}</span>}</td><td>{Object.entries(r.metrics?.models ?? {}).map(([k, n]) => `${k}×${n}`).join(", ")}</td>
        <td><button className="secondary" onClick={() => setRunId(r.id)}>View</button></td></tr>)}</tbody></table>
      {(runs.data ?? []).length === 0 && <p className="muted">No forecast runs yet.</p>}</div>
    {run?.status === "completed" && <div className="card"><div className="row"><select value={vid} onChange={e => setVariant(e.target.value)}>{variants.map((x: any) => <option key={x.id} value={x.id}>{x.label}</option>)}</select></div>
      <Async q={res}>{() => (<>
        <ResponsiveContainer width="100%" height={300}><ComposedChart data={chart}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="date" minTickGap={30} /><YAxis /><Tooltip /><Legend />
          <Area dataKey="band" name="80% interval" stroke="none" fill="#c7d2fe" /><Line dataKey="actual" name="Actual" stroke="#101828" dot={false} /><Line dataKey="predicted" name="Predicted" stroke="#4f46e5" dot={false} /></ComposedChart></ResponsiveContainer>
        {meta && <div><p><b>Model:</b> {res.data.results[0].model_name} · <b>Data:</b> {meta.sufficiency} ({meta.training_points} days)</p>
          {meta.evaluation?.length > 0 && <table><thead><tr><th>Candidate</th><th>WAPE</th><th>MAE</th><th>RMSE</th><th>Bias</th></tr></thead><tbody>{meta.evaluation.map((e: any) =>
            <tr key={e.model}><td>{e.model}</td><td>{e.wape == null ? "n/a" : fmt(e.wape)}</td><td>{fmt(e.mae)}</td><td>{fmt(e.rmse)}</td><td>{e.bias == null ? "n/a" : fmt(e.bias)}</td></tr>)}</tbody></table>}
          {meta.warnings?.map((w: string) => <p className="err" key={w}>⚠ {w}</p>)}</div>}</>)}</Async></div>}</>);
}

/* ------------------------------------------------------------ replenishment */
function Replenishment() {
  const b = useBranch(); const qc = useQueryClient(); const toast = useToast();
  const [budget, setBudget] = useState(""); const [open, setOpen] = useState<string>("");
  const q = useQuery({ queryKey: ["recs", b.branch], queryFn: () => api(`/replenishment/recommendations?branch_id=${b.branch}&status=`), enabled: !!b.branch });
  const refresh = () => qc.invalidateQueries({ queryKey: ["recs"] });
  const gen = useMutation({ mutationFn: () => api("/replenishment/recommendations/generate", { body: { branch_id: b.branch, budget: budget ? +budget : null } }), onSuccess: r => { toast(`${r.length} recommendation(s)`); refresh(); }, onError: e => toast(errMsg(e)) });
  const decide = useMutation({ mutationFn: ({ id, a }: { id: string; a: string }) => api(`/replenishment/recommendations/${id}/${a}`, { method: "POST" }), onSuccess: refresh, onError: e => toast(errMsg(e)) });
  const conv = useMutation({ mutationFn: (ids: string[]) => api("/replenishment/recommendations/convert", { body: { recommendation_ids: ids } }), onSuccess: r => { toast(`${r.length} draft purchase order(s) created`); refresh(); qc.invalidateQueries({ queryKey: ["pos"] }); }, onError: e => toast(errMsg(e)) });
  const rows: any[] = q.data ?? []; const accepted = rows.filter(r => r.status === "accepted").map(r => r.id);
  return (<><h2>Replenishment Intelligence</h2><div className="card"><div className="row"><BranchPicker b={b} />
    <input placeholder="Optional budget" style={{ width: 150 }} value={budget} onChange={e => setBudget(e.target.value)} />
    <button disabled={!b.branch || gen.isPending} onClick={() => gen.mutate()}>Refresh recommendations</button>
    <button className="secondary" disabled={accepted.length === 0} onClick={() => window.confirm(`Create draft purchase order(s) from ${accepted.length} accepted recommendation(s)? Nothing is sent to suppliers.`) && conv.mutate(accepted)}>Convert accepted to draft PO</button></div></div>
    <div className="card"><Async q={q}>{() => rows.length === 0 ? <p className="muted">No recommendations. Add supplier terms, record sales, then refresh.</p> :
      <table><thead><tr><th>Urgency</th><th>Variant</th><th>Qty</th><th>Est. cost</th><th>Status</th><th /></tr></thead><tbody>{rows.map(r => (<>
        <tr key={r.id}><td><span className={`pill ${r.urgency}`}>{r.urgency}</span></td><td>{r.variant_id.slice(0, 8)}</td><td>{fmt(r.recommended_quantity)}</td><td>{fmt(r.estimated_cost)}</td><td>{r.status}</td>
          <td className="row"><button className="secondary" onClick={() => setOpen(open === r.id ? "" : r.id)}>Why?</button>
            {r.status === "pending" && <><button onClick={() => decide.mutate({ id: r.id, a: "accept" })}>Accept</button><button className="secondary" onClick={() => decide.mutate({ id: r.id, a: "reject" })}>Reject</button></>}</td></tr>
        {open === r.id && <tr key={r.id + "x"}><td colSpan={6}><p>{r.reason}</p>{r.details.warnings?.map((w: string) => <p className="err" key={w}>⚠ {w}</p>)}
          <p className="muted">Lead time {r.details.lead_time_days}d · MOQ {r.details.moq} · pack {r.details.pack_size} · forecast source: {r.details.forecast_source}</p></td></tr>}</>))}</tbody></table>}</Async></div></>);
}

/* ------------------------------------------------------------------ health */
function Health() {
  const b = useBranch();
  const q = useQuery({ queryKey: ["health", b.branch], queryFn: () => api(`/analytics/inventory-health?branch_id=${b.branch}`), enabled: !!b.branch });
  return (<><h2>Inventory Health</h2><div className="row"><BranchPicker b={b} />
    <button className="secondary" disabled={!b.branch} onClick={() => download(`/reports/inventory.xlsx?branch_id=${b.branch}`, "stocksense-inventory.xlsx").catch(() => alert("Could not download the report"))}>Export to Excel</button></div><Async q={q}>{(d) => (<>
    <div className="grid">{[["Valuation (cost)", d.summary.inventory_valuation_at_cost], ["Unavailable stock (cost)", d.summary.unavailable_stock_value_at_cost], ["Slow-moving", d.summary.slow_moving_count],
      ["Overstocked", d.summary.overstock_count], ["Dead-stock estimate", d.summary.dead_stock_value_estimate], ["Turnover (period)", d.summary.turnover_period]].map(([l, v]) =>
      <div className="card stat" key={String(l)}><b>{fmt(v)}</b><span>{l}</span></div>)}</div>
    <p className="muted">{d.summary.turnover_note}</p>
    <div className="card"><table><thead><tr><th>SKU</th><th>Name</th><th>On hand</th><th>Status</th><th>Days of inventory</th><th>ABC</th><th>Last sale</th></tr></thead><tbody>{d.items.map((i: any) =>
      <tr key={i.variant_id}><td>{i.sku}</td><td>{i.name}</td><td>{fmt(i.on_hand)}</td><td>{i.status}{i.dead_stock_estimate && " · dead-stock est."}</td><td>{fmt(i.days_of_inventory)}</td><td>{i.abc}</td><td>{i.last_sale ?? "never"}</td></tr>)}</tbody></table></div></>)}</Async></>);
}

/* --------------------------------------------------------------------- app */
export default function App() {
  const [msg, setMsg] = useState("");
  const toast = (m: string) => { setMsg(m); setTimeout(() => setMsg(""), 3500); };
  const authed = !!session.token;
  return (<Toast.Provider value={toast}>
    <Routes>
      <Route path="/login" element={<AuthPage />} />
      <Route path="/onboarding" element={authed ? <Onboarding /> : <Navigate to="/login" />} />
      {authed && session.org ? <Route path="/*" element={<Shell><Routes>
        <Route path="/" element={<Overview />} /><Route path="/products" element={<Products />} /><Route path="/sales" element={<Sales />} />
        <Route path="/purchasing" element={<Purchasing />} /><Route path="/forecasting" element={<Forecasting />} />
        <Route path="/replenishment" element={<Replenishment />} /><Route path="/health" element={<Health />} /></Routes></Shell>} />
        : <Route path="/*" element={<Navigate to={authed ? "/onboarding" : "/login"} />} />}
    </Routes>{msg && <div className="toast" role="status">{msg}</div>}</Toast.Provider>);
}
