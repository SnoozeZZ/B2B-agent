import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  api,
  clearAccessToken,
  downloadFile,
  onAuthExpired,
  setAccessToken,
} from "./api";
import type {
  AuditLog,
  AuthSession,
  Customer,
  CustomerMatch,
  Dashboard,
  Distribution,
  Inquiry,
  InventoryItem,
  OpsTask,
  Order,
  Quote,
  User,
} from "./types";

type Section = "dashboard" | "inquiries" | "tasks" | "customers" | "quotes" | "distributions" | "orders" | "audit" | "users";

const statusLabel: Record<string, string> = {
  received: "已接收",
  need_info: "待补全",
  need_ops: "待运营",
  ready_to_quote: "可报价",
  quoted: "已报价",
  distributed: "已分发",
  open: "待处理",
  in_progress: "进行中",
  resolved: "已解决",
  draft: "草稿",
  pending: "待确认",
  pending_confirmation: "待确认",
  confirmed: "已确认",
  paid: "已付款",
  partially_paid: "部分付款",
  partial: "部分付款",
  unpaid: "未付款",
  processing: "履约中",
  unfulfilled: "未履约",
  fulfilled: "已履约",
  shipped: "已发货",
  delivered: "已送达",
  completed: "已完成",
  cancelled: "已取消",
  queued: "队列中",
  sending: "发送中",
  retry_scheduled: "等待重试",
  sent: "已发送",
  failed: "失败",
};

const fieldLabel: Record<string, string> = {
  brand: "品牌",
  sku: "SKU/货号",
  purchase_price: "采购价",
  currency: "币种",
  moq: "MOQ",
};

function Badge({ value }: { value: string }) {
  return <span className={`badge badge-${value}`}>{statusLabel[value] ?? value}</span>;
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);

  useEffect(() => {
    const expire = () => setUser(null);
    const unsubscribe = onAuthExpired(expire);
    api<User>("/auth/me")
      .then(setUser)
      .catch((reason) => {
        if (!(reason instanceof ApiError) || reason.status !== 401) {
          // A login attempt will expose a clearer connectivity error.
        }
        setUser(null);
      })
      .finally(() => setCheckingSession(false));
    return unsubscribe;
  }, []);

  if (checkingSession) return <AuthLoading />;
  if (!user) return <LoginView onAuthenticated={setUser} />;

  return (
    <Console
      key={`${user.username}-${user.role}`}
      user={user}
      onLogout={() => {
        clearAccessToken();
        setUser(null);
      }}
    />
  );
}

function AuthLoading() {
  return (
    <main className="auth-shell">
      <div className="auth-card auth-loading">
        <div className="brand-mark">B2B</div>
        <p>正在验证会话…</p>
      </div>
    </main>
  );
}

function LoginView({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setLoading(true);
    setError(null);
    try {
      const session = await api<AuthSession>("/auth/login", {
        method: "POST",
        body: JSON.stringify({
          username: String(form.get("username") ?? "").trim(),
          password: String(form.get("password") ?? ""),
        }),
      });
      setAccessToken(session.access_token, form.get("remember") === "on");
      onAuthenticated(session.user);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card">
        <div className="auth-brand">
          <div className="brand-mark">B2B</div>
          <div>
            <strong>分销运营台</strong>
            <span>Distribution OS</span>
          </div>
        </div>
        <p className="eyebrow">安全登录</p>
        <h1>欢迎回来</h1>
        <p className="auth-intro">登录后可按角色访问询价、报价、订单与审计数据。</p>
        {error ? <div className="alert alert-error"><span>{error}</span></div> : null}
        <form className="stack-form auth-form" onSubmit={login}>
          <label>用户名<input name="username" autoComplete="username" required autoFocus /></label>
          <label>密码<input name="password" type="password" autoComplete="current-password" required /></label>
          <label className="checkbox-label">
            <input name="remember" type="checkbox" />
            在此设备保持登录
          </label>
          <button className="primary-button" type="submit" disabled={loading}>
            {loading ? "登录中…" : "登录"}
          </button>
        </form>
        <small className="auth-note">未勾选时，凭证只保存在当前浏览器会话中。</small>
      </section>
    </main>
  );
}

function Console({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [section, setSection] = useState<Section>("dashboard");
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [inquiries, setInquiries] = useState<Inquiry[]>([]);
  const [tasks, setTasks] = useState<OpsTask[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [distributions, setDistributions] = useState<Distribution[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [inventory, setInventory] = useState<InventoryItem[]>([]);
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [selected, setSelected] = useState<Inquiry | null>(null);
  const [selectedOrder, setSelectedOrder] = useState<Order | null>(null);
  const [matches, setMatches] = useState<CustomerMatch[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  const canOperate = user.role === "admin" || user.role === "operator";

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const loaders: Promise<unknown>[] = [
      api<Dashboard>("/dashboard").then(setDashboard),
      api<Inquiry[]>("/inquiries").then((items) => {
        setInquiries(items);
        setSelected((current) => current ? items.find((item) => item.id === current.id) ?? null : null);
      }),
      api<Customer[]>("/customers").then(setCustomers),
      api<Quote[]>("/quotes").then(setQuotes),
      api<Distribution[]>("/distributions").then(setDistributions),
      api<Order[]>("/orders").then((items) => {
        setOrders(items);
        setSelectedOrder((current) => current ? items.find((item) => item.id === current.id) ?? current : null);
      }),
      api<InventoryItem[]>("/inventory").then(setInventory),
    ];
    if (canOperate) {
      loaders.push(api<OpsTask[]>("/ops-tasks").then(setTasks));
      loaders.push(api<AuditLog[]>("/audit-logs?limit=100").then(setAuditLogs));
    }
    if (user.role === "admin") loaders.push(api<User[]>("/users").then(setUsers));
    const results = await Promise.allSettled(loaders);
    const failure = results.find((result): result is PromiseRejectedResult => result.status === "rejected");
    if (failure) setError(failure.reason instanceof Error ? failure.reason.message : "部分数据加载失败");
    setLoading(false);
  }, [canOperate, user.role]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selected) {
      setMatches([]);
      return;
    }
    api<CustomerMatch[]>(`/inquiries/${selected.id}/matches`)
      .then(setMatches)
      .catch(() => setMatches([]));
  }, [
    selected?.id,
    selected?.brand,
    selected?.sku,
    selected?.industry,
    selected?.target_region,
    selected?.delivery_location,
    customers.length,
  ]);

  function flash(message: string) {
    setNotice(message);
    window.setTimeout(() => setNotice(null), 3000);
  }

  async function createInquiry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    try {
      const inquiry = await api<Inquiry>("/inquiries", {
        method: "POST",
        body: JSON.stringify({
          channel: "manual",
          sender_name: String(form.get("sender_name") ?? ""),
          content: String(form.get("content") ?? ""),
        }),
      });
      formElement.reset();
      flash(`询价 #${inquiry.id} 已创建`);
      await refresh();
      setSelected(inquiry);
      setSection("inquiries");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建失败");
    }
  }

  async function importExcel(file: File | undefined) {
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    try {
      const result = await api<Inquiry[]>("/inquiries/import", { method: "POST", body });
      flash(`已从 Excel 导入 ${result.length} 条询价`);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "导入失败");
    }
  }

  async function saveInquiry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const form = new FormData(event.currentTarget);
    const value = (name: string) => String(form.get(name) ?? "").trim() || null;
    try {
      const updated = await api<Inquiry>(`/inquiries/${selected.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          brand: value("brand"),
          sku: value("sku"),
          industry: value("industry"),
          target_region: value("target_region"),
          purchase_price: value("purchase_price"),
          currency: value("currency"),
          moq: value("moq") ? Number(value("moq")) : null,
          delivery_location: value("delivery_location"),
          trade_term: value("trade_term"),
          payment_terms: value("payment_terms"),
          lead_time: value("lead_time"),
          stock_type: value("stock_type"),
        }),
      });
      setSelected(updated);
      flash("询价字段已保存");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    }
  }

  async function createQuoteForSelected(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const form = new FormData(event.currentTarget);
    try {
      const quote = await api<Quote>(`/inquiries/${selected.id}/quotes`, {
        method: "POST",
        body: JSON.stringify({
          scenario: form.get("scenario"),
          output_currency: form.get("output_currency"),
          fx_rate: String(form.get("fx_rate") ?? "").trim() || null,
          freight_per_unit: String(form.get("freight_per_unit") ?? "0"),
        }),
      });
      flash(`报价 #${quote.id} 已生成，单价 ${quote.output_currency} ${quote.unit_price}`);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "报价生成失败");
    }
  }

  async function createCustomer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const list = (name: string) => String(form.get(name) ?? "")
      .split(/[,，\n]/)
      .map((item) => item.trim())
      .filter(Boolean);
    try {
      await api<Customer>("/customers", {
        method: "POST",
        body: JSON.stringify({
          name: form.get("name"),
          email: String(form.get("email") ?? "").trim() || null,
          channel: form.get("channel"),
          channel_target: String(form.get("channel_target") ?? "").trim() || null,
          brands: list("brands"),
          industries: list("industries"),
          regions: list("regions"),
          sku_patterns: list("sku_patterns"),
          match_threshold: Number(form.get("match_threshold") ?? 45),
        }),
      });
      formElement.reset();
      flash("客户及匹配偏好已保存");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "客户创建失败");
    }
  }

  async function resolveTask(task: OpsTask) {
    try {
      await api(`/ops-tasks/${task.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "resolved", assignee: user.username }),
      });
      flash(`待办 #${task.id} 已解决`);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "待办更新失败");
    }
  }

  const latestSelectedQuote = useMemo(
    () => quotes.find((quote) => quote.inquiry_id === selected?.id),
    [quotes, selected?.id],
  );

  async function distribute(customerId: number) {
    if (!latestSelectedQuote) return;
    try {
      await api(`/quotes/${latestSelectedQuote.id}/distributions`, {
        method: "POST",
        body: JSON.stringify({ customer_ids: [customerId], confirm: true }),
      });
      flash("分发任务已确认并进入发送队列");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "分发创建失败");
    }
  }

  async function downloadQuote(quote: Quote, format: "xlsx" | "pdf") {
    const key = `${quote.id}-${format}`;
    setDownloading(key);
    try {
      await downloadFile(
        `/quotes/${quote.id}/export.${format}`,
        `quote-${quote.id}-v${quote.version}.${format}`,
      );
      flash(`${format.toUpperCase()} 报价单已下载`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "报价单下载失败");
    } finally {
      setDownloading(null);
    }
  }

  async function loadOrder(orderId: number) {
    try {
      setSelectedOrder(await api<Order>(`/orders/${orderId}`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "订单详情加载失败");
    }
  }

  async function createOrder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const element = event.currentTarget;
    const form = new FormData(element);
    const quoteId = String(form.get("quote_id") ?? "").trim();
    try {
      const order = await api<Order>("/orders", {
        method: "POST",
        body: JSON.stringify({
          customer_id: Number(form.get("customer_id")),
          quote_id: quoteId ? Number(quoteId) : null,
          currency: String(form.get("currency") ?? "CNY").toUpperCase(),
          shipping_address: String(form.get("shipping_address") ?? "").trim() || null,
          notes: String(form.get("notes") ?? "").trim() || null,
          items: [{
            sku: String(form.get("sku") ?? "").trim(),
            brand: String(form.get("brand") ?? "").trim() || null,
            description: String(form.get("description") ?? "").trim() || null,
            quantity: Number(form.get("quantity")),
            unit_price: String(form.get("unit_price") ?? "0"),
          }],
        }),
      });
      element.reset();
      flash(`订单 ${order.order_number ?? `#${order.id}`} 已创建`);
      await refresh();
      await loadOrder(order.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "订单创建失败");
    }
  }

  async function recordPayment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedOrder) return;
    const element = event.currentTarget;
    const form = new FormData(element);
    try {
      await api(`/orders/${selectedOrder.id}/payments`, {
        method: "POST",
        body: JSON.stringify({
          amount: String(form.get("amount") ?? "0"),
          currency: String(form.get("currency") ?? selectedOrder.currency).toUpperCase(),
          method: form.get("method"),
          status: form.get("status"),
          transaction_reference: String(form.get("transaction_reference") ?? "").trim() || null,
        }),
      });
      element.reset();
      flash("付款记录已保存");
      await refresh();
      await loadOrder(selectedOrder.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "付款记录失败");
    }
  }

  async function updateOrderStatus(status: "confirmed" | "cancelled" | "completed") {
    if (!selectedOrder) return;
    try {
      const updated = await api<Order>(`/orders/${selectedOrder.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      });
      setSelectedOrder(updated);
      flash(status === "confirmed" ? "订单已确认并预留库存" : status === "cancelled" ? "订单已取消" : "订单已完成");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "订单状态更新失败");
    }
  }

  async function recordShipment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedOrder) return;
    const element = event.currentTarget;
    const form = new FormData(element);
    try {
      await api(`/orders/${selectedOrder.id}/shipments`, {
        method: "POST",
        body: JSON.stringify({
          carrier: String(form.get("carrier") ?? "").trim(),
          tracking_number: String(form.get("tracking_number") ?? "").trim() || null,
          status: form.get("status"),
        }),
      });
      element.reset();
      flash("物流记录已保存");
      await refresh();
      await loadOrder(selectedOrder.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "物流记录失败");
    }
  }

  async function updateShipmentStatus(shipmentId: number, status: "in_transit" | "delivered" | "exception") {
    if (!selectedOrder) return;
    try {
      await api(`/shipments/${shipmentId}/events`, {
        method: "POST",
        body: JSON.stringify({ status }),
      });
      flash("物流状态已更新");
      await refresh();
      await loadOrder(selectedOrder.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "物流状态更新失败");
    }
  }

  async function adjustInventory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const element = event.currentTarget;
    const form = new FormData(element);
    const sku = String(form.get("sku") ?? "").trim();
    try {
      await api(`/inventory/${encodeURIComponent(sku)}/adjust`, {
        method: "POST",
        body: JSON.stringify({
          quantity: Number(form.get("quantity")),
          movement_type: form.get("movement_type"),
          warehouse: String(form.get("warehouse") ?? "").trim() || null,
          note: String(form.get("note") ?? "").trim() || null,
          brand: String(form.get("brand") ?? "").trim() || null,
          name: String(form.get("name") ?? "").trim() || null,
          reorder_point: String(form.get("reorder_point") ?? "").trim() ? Number(form.get("reorder_point")) : null,
        }),
      });
      element.reset();
      flash(`库存 ${sku} 已调整`);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "库存调整失败");
    }
  }

  async function createUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const element = event.currentTarget;
    const form = new FormData(element);
    try {
      await api<User>("/users", {
        method: "POST",
        body: JSON.stringify({
          username: String(form.get("username") ?? "").trim(),
          display_name: String(form.get("display_name") ?? "").trim(),
          password: String(form.get("password") ?? ""),
          role: form.get("role"),
        }),
      });
      element.reset();
      flash("用户账号已创建");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "用户创建失败");
    }
  }

  async function retryDistribution(item: Distribution) {
    try {
      await api<Distribution>(`/distributions/${item.id}/retry`, { method: "POST" });
      flash(`分发任务 #${item.id} 已重新入队`);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重新入队失败");
    }
  }

  async function toggleUser(target: User) {
    if (target.id === undefined) return;
    try {
      await api<User>(`/users/${target.id}`, {
        method: "PATCH",
        body: JSON.stringify({ active: !target.active }),
      });
      flash(target.active ? "账号已停用" : "账号已启用");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "账号状态更新失败");
    }
  }

  const navItems: [Section, string][] = [
    ["dashboard", "总览"],
    ["inquiries", "询价中心"],
    ...(canOperate ? [["tasks", "运营待办"]] as [Section, string][] : []),
    ["customers", "客户与品牌"],
    ["quotes", "报价记录"],
    ["distributions", "分发任务"],
    ["orders", "订单与履约"],
    ...(canOperate ? [["audit", "审计日志"]] as [Section, string][] : []),
    ...(user.role === "admin" ? [["users", "用户管理"]] as [Section, string][] : []),
  ];

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-mark">B2B</div>
        <div className="brand-copy"><strong>分销运营台</strong><span>Distribution OS</span></div>
        <nav>
          {navItems.map(([key, label]) => (
            <button key={key} className={section === key ? "active" : ""} onClick={() => setSection(key)}>
              {label}
              {key === "tasks" && dashboard?.open_tasks ? <span className="nav-count">{dashboard.open_tasks}</span> : null}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer"><span className="status-dot" /> 系统正常</div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div><p className="eyebrow">运营工作空间</p><h1>{sectionTitle(section)}</h1></div>
          <div className="topbar-actions">
            <button className="ghost-button" onClick={() => void refresh()} disabled={loading}>{loading ? "刷新中…" : "刷新数据"}</button>
            <div className="user-menu">
              <span><strong>{user.display_name || user.name || user.username}</strong><small>{roleLabel(user.role)}</small></span>
              <button className="link-button" onClick={onLogout}>退出</button>
            </div>
          </div>
        </header>

        {error ? <div className="alert alert-error"><span>{error}</span><button onClick={() => setError(null)}>关闭</button></div> : null}
        {notice ? <div className="toast">{notice}</div> : null}

        {section === "dashboard" && dashboard ? (
          <DashboardView
            dashboard={dashboard}
            inquiries={inquiries}
            tasks={tasks}
            onOpen={(inquiry) => { setSelected(inquiry); setSection("inquiries"); }}
          />
        ) : null}

        {section === "inquiries" ? (
          <div className="split-layout">
            <section className="panel list-panel">
              <div className="panel-head">
                <div><h2>询价队列</h2><p>{inquiries.length} 条记录</p></div>
                {canOperate ? <label className="file-button">导入 Excel<input type="file" accept=".xlsx" onChange={(e) => void importExcel(e.target.files?.[0])} /></label> : null}
              </div>
              <div className="record-list">
                {inquiries.map((item) => (
                  <button key={item.id} className={`record-card ${selected?.id === item.id ? "selected" : ""}`} onClick={() => setSelected(item)}>
                    <div className="record-top"><strong>{item.brand || "待识别品牌"}</strong><Badge value={item.status} /></div>
                    <span>{item.sku || "SKU 待补充"}</span>
                    <small>{item.sender_name} · {formatTime(item.created_at)}</small>
                  </button>
                ))}
                {!inquiries.length ? <Empty text="还没有询价" /> : null}
              </div>
            </section>
            <section className="panel detail-panel">
              {selected ? (
                <InquiryDetail
                  inquiry={selected}
                  matches={matches}
                  quote={latestSelectedQuote}
                  canOperate={canOperate}
                  downloading={downloading}
                  onSave={saveInquiry}
                  onQuote={createQuoteForSelected}
                  onDownload={downloadQuote}
                  onDistribute={distribute}
                />
              ) : canOperate ? <CreateInquiryForm onSubmit={createInquiry} /> : <Empty text="请选择一条询价查看详情" />}
            </section>
          </div>
        ) : null}

        {section === "tasks" && canOperate ? (
          <section className="panel">
            <div className="panel-head"><div><h2>运营待办</h2><p>异常、缺失和人工确认统一处理</p></div></div>
            <div className="table-wrap"><table>
              <thead><tr><th>任务</th><th>询价</th><th>状态</th><th>建议回复</th><th>操作</th></tr></thead>
              <tbody>{tasks.map((task) => (
                <tr key={task.id}>
                  <td><strong>{task.title}</strong><small>{task.task_type}</small></td><td>#{task.inquiry_id}</td><td><Badge value={task.status} /></td>
                  <td className="wide-cell">{task.suggested_reply || "—"}</td>
                  <td>{task.status !== "resolved" ? <button className="link-button" onClick={() => void resolveTask(task)}>标记解决</button> : "—"}</td>
                </tr>
              ))}</tbody>
            </table></div>
          </section>
        ) : null}

        {section === "customers" ? (
          <div className="split-layout customer-layout">
            <section className="panel">
              <div className="panel-head"><div><h2>客户与匹配偏好</h2><p>综合品牌、SKU、行业和区域进行匹配</p></div></div>
              <div className="customer-grid">
                {customers.map((customer) => <CustomerCard customer={customer} key={customer.id} />)}
                {!customers.length ? <Empty text="尚未维护客户" /> : null}
              </div>
            </section>
            {canOperate ? (
              <section className="panel form-panel">
                <h2>新增客户</h2>
                <form className="stack-form" onSubmit={createCustomer}>
                  <label>客户名称<input name="name" required /></label>
                  <label>邮箱<input name="email" type="email" /></label>
                  <label>接收渠道<select name="channel"><option value="email">邮箱</option><option value="wecom">企业微信</option><option value="feishu">飞书</option></select></label>
                  <label>渠道目标<input name="channel_target" placeholder="群 ID 或联系人 ID" /></label>
                  <label>关注品牌<input name="brands" placeholder="Nike, Adidas" /></label>
                  <label>SKU 模式<input name="sku_patterns" placeholder="DD*, WM-??；支持逗号分隔" /></label>
                  <label>行业<input name="industries" placeholder="运动服饰, 零售" /></label>
                  <label>区域<input name="regions" placeholder="华东, 上海" /></label>
                  <label>最低匹配分<input name="match_threshold" type="number" min="0" max="100" defaultValue="45" /></label>
                  <button className="primary-button" type="submit">保存客户</button>
                </form>
              </section>
            ) : null}
          </div>
        ) : null}

        {section === "quotes" ? (
          <section className="panel">
            <div className="panel-head"><div><h2>报价快照</h2><p>下载已脱敏的 Excel 或 PDF 外发版本</p></div></div>
            <div className="table-wrap"><table>
              <thead><tr><th>报价</th><th>询价</th><th>场景</th><th>汇率</th><th>单价</th><th>脱敏</th><th>创建时间</th><th>文件</th></tr></thead>
              <tbody>{quotes.map((quote) => (
                <tr key={quote.id}>
                  <td><strong>#{quote.id}</strong><small>v{quote.version}</small></td><td>#{quote.inquiry_id}</td><td>{quote.scenario}</td><td>{quote.fx_rate}</td>
                  <td><strong>{quote.output_currency} {quote.unit_price}</strong></td>
                  <td><span className={quote.sensitive_scan_passed ? "safe" : "danger"}>{quote.sensitive_scan_passed ? "已通过" : "未通过"}</span></td>
                  <td>{formatTime(quote.created_at)}</td>
                  <td><div className="action-row">
                    <button className="link-button" disabled={downloading !== null || !quote.sensitive_scan_passed} onClick={() => void downloadQuote(quote, "xlsx")}>{downloading === `${quote.id}-xlsx` ? "下载中…" : "Excel"}</button>
                    <button className="link-button" disabled={downloading !== null || !quote.sensitive_scan_passed} onClick={() => void downloadQuote(quote, "pdf")}>{downloading === `${quote.id}-pdf` ? "下载中…" : "PDF"}</button>
                  </div></td>
                </tr>
              ))}</tbody>
            </table></div>
          </section>
        ) : null}

        {section === "distributions" ? (
          <section className="panel">
            <div className="panel-head"><div><h2>分发任务</h2><p>渠道发送、重试与送达状态</p></div></div>
            <div className="table-wrap"><table><thead><tr><th>任务</th><th>客户</th><th>渠道</th><th>状态</th><th>尝试</th><th>服务商消息</th><th>错误</th><th>时间</th><th>操作</th></tr></thead><tbody>
              {distributions.map((item) => <tr key={item.id}><td><strong>#{item.id}</strong><small>报价 #{item.quote_id}</small></td><td>{item.customer_name}</td><td>{item.channel}</td><td><Badge value={item.status} /></td><td>{item.attempt_count}/{item.max_attempts}</td><td>{item.provider_message_id || "—"}</td><td className="wide-cell">{item.error_message || "—"}</td><td>{formatMaybeTime(item.delivered_at || item.sent_at || item.created_at)}</td><td>{canOperate && ["failed", "retry_scheduled"].includes(item.status) ? <button className="link-button" onClick={() => void retryDistribution(item)}>重试</button> : "—"}</td></tr>)}
            </tbody></table></div>{!distributions.length ? <Empty text="暂无分发任务" /> : null}
          </section>
        ) : null}

        {section === "orders" ? (
          <OrdersView
            orders={orders}
            selected={selectedOrder}
            inventory={inventory}
            customers={customers}
            quotes={quotes}
            canOperate={canOperate}
            onSelect={(id) => void loadOrder(id)}
            onCreate={createOrder}
            onPayment={recordPayment}
            onShipment={recordShipment}
            onShipmentStatus={updateShipmentStatus}
            onStatusChange={updateOrderStatus}
            onInventoryAdjust={adjustInventory}
          />
        ) : null}

        {section === "audit" && canOperate ? (
          <section className="panel">
            <div className="panel-head"><div><h2>审计日志</h2><p>带完整性校验的追加式业务记录</p></div></div>
            <div className="table-wrap"><table>
              <thead><tr><th>时间</th><th>实体</th><th>动作</th><th>操作人</th><th>上下文</th></tr></thead>
              <tbody>{auditLogs.map((log) => (
                <tr key={log.id}><td>{formatTime(log.created_at)}</td><td><strong>{log.entity_type}</strong><small>#{log.entity_id}</small></td><td>{log.action}</td><td>{log.actor}</td><td className="wide-cell audit-payload">{JSON.stringify(log.payload)}</td></tr>
              ))}</tbody>
            </table></div>
          </section>
        ) : null}

        {section === "users" && user.role === "admin" ? (
          <div className="split-layout customer-layout">
            <section className="panel">
              <div className="panel-head"><div><h2>用户账号</h2><p>管理员、运营和只读角色</p></div></div>
              <div className="table-wrap"><table><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>最近登录</th><th>操作</th></tr></thead><tbody>
                {users.map((item) => <tr key={item.id}><td><strong>{item.display_name || item.username}</strong><small>{item.username}</small></td><td>{roleLabel(item.role)}</td><td>{item.active ? "启用" : "停用"}</td><td>{formatMaybeTime(item.last_login_at)}</td><td><button className="link-button" disabled={item.id === user.id} onClick={() => void toggleUser(item)}>{item.active ? "停用" : "启用"}</button></td></tr>)}
              </tbody></table></div>
            </section>
            <section className="panel form-panel"><h2>新增用户</h2><form className="stack-form" onSubmit={createUser}>
              <label>用户名<input name="username" minLength={3} pattern="[A-Za-z0-9_.-]+" required /></label>
              <label>显示名称<input name="display_name" required /></label>
              <label>初始密码<input name="password" type="password" minLength={10} required /></label>
              <label>角色<select name="role"><option value="operator">运营</option><option value="viewer">只读</option><option value="admin">管理员</option></select></label>
              <button className="primary-button" type="submit">创建账号</button>
            </form></section>
          </div>
        ) : null}
      </main>
    </div>
  );
}

function CustomerCard({ customer }: { customer: Customer }) {
  const groups = [
    ["品牌", customer.brands],
    ["SKU", customer.sku_patterns],
    ["行业", customer.industries],
    ["区域", customer.regions],
  ] as const;
  return (
    <article className="customer-card">
      <div><strong>{customer.name}</strong><Badge value={customer.channel} /></div>
      <p>{customer.email || customer.channel_target || "未配置接收地址"}</p>
      {groups.map(([label, values]) => values?.length ? (
        <div className="preference-row" key={label}><small>{label}</small><div className="tag-row">{values.map((value) => <span className="tag" key={value}>{value}</span>)}</div></div>
      ) : null)}
      <small className="threshold">匹配阈值：{customer.match_threshold ?? 45}</small>
    </article>
  );
}

function OrdersView({ orders, selected, inventory, customers, quotes, canOperate, onSelect, onCreate, onPayment, onShipment, onShipmentStatus, onStatusChange, onInventoryAdjust }: {
  orders: Order[];
  selected: Order | null;
  inventory: InventoryItem[];
  customers: Customer[];
  quotes: Quote[];
  canOperate: boolean;
  onSelect: (id: number) => void;
  onCreate: (event: FormEvent<HTMLFormElement>) => void;
  onPayment: (event: FormEvent<HTMLFormElement>) => void;
  onShipment: (event: FormEvent<HTMLFormElement>) => void;
  onShipmentStatus: (shipmentId: number, status: "in_transit" | "delivered" | "exception") => void;
  onStatusChange: (status: "confirmed" | "cancelled" | "completed") => void;
  onInventoryAdjust: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <div className="orders-stack">
      {canOperate ? (
        <section className="panel order-create-panel">
          <div className="panel-head"><div><h2>创建订单</h2><p>从已确认报价建立下游订单与明细</p></div></div>
          <form className="order-create-form" onSubmit={onCreate}>
            <label>客户<select name="customer_id" required defaultValue=""><option value="" disabled>选择客户</option>{customers.map((customer) => <option key={customer.id} value={customer.id}>{customer.name}</option>)}</select></label>
            <label>关联报价<select name="quote_id" defaultValue=""><option value="">不关联</option>{quotes.map((quote) => <option key={quote.id} value={quote.id}>#{quote.id} · {quote.output_currency} {quote.unit_price}</option>)}</select></label>
            <label>SKU<input name="sku" required /></label>
            <label>品牌<input name="brand" /></label>
            <label>描述<input name="description" /></label>
            <label>数量<input name="quantity" type="number" min="1" required /></label>
            <label>成交单价<input name="unit_price" type="number" min="0" step="0.01" required /></label>
            <label>币种<input name="currency" defaultValue="CNY" required /></label>
            <label>收货地址<input name="shipping_address" /></label>
            <label className="order-notes">备注<input name="notes" /></label>
            <button className="primary-button" type="submit" disabled={!customers.length}>创建订单</button>
          </form>
        </section>
      ) : null}

      <div className="split-layout order-layout">
        <section className="panel list-panel">
          <div className="panel-head"><div><h2>订单</h2><p>{orders.length} 条记录</p></div></div>
          <div className="record-list order-list">
            {orders.map((order) => (
              <button key={order.id} className={`record-card ${selected?.id === order.id ? "selected" : ""}`} onClick={() => onSelect(order.id)}>
                <div className="record-top"><strong>{order.order_number ?? `订单 #${order.id}`}</strong><Badge value={order.status} /></div>
                <span>{order.customer_name ?? `客户 #${order.customer_id}`} · {order.currency} {order.total_amount}</span>
                <small>{order.items?.length ?? 0} 个明细 · {formatTime(order.created_at)}</small>
              </button>
            ))}
            {!orders.length ? <Empty text="还没有订单" /> : null}
          </div>
        </section>
        <section className="panel order-detail-panel">
          {selected ? <OrderDetail order={selected} canOperate={canOperate} onPayment={onPayment} onShipment={onShipment} onShipmentStatus={onShipmentStatus} onStatusChange={onStatusChange} /> : <Empty text="请选择订单查看付款、物流与明细" />}
        </section>
      </div>

      <section className="panel">
        <div className="panel-head"><div><h2>库存</h2><p>现存、预留与可用数量</p></div></div>
        {canOperate ? (
          <form className="inventory-form" onSubmit={onInventoryAdjust}>
            <label>SKU<input name="sku" list="inventory-skus" required /><datalist id="inventory-skus">{inventory.map((item) => <option key={item.sku} value={item.sku} />)}</datalist></label>
            <label>数量调整<input name="quantity" type="number" required placeholder="增加为正，减少为负" /></label>
            <label>变动类型<select name="movement_type"><option value="receipt">采购入库</option><option value="adjustment">盘点调整</option><option value="damage">破损扣减</option><option value="release">释放预留</option></select></label>
            <label>仓库<input name="warehouse" placeholder="默认仓" /></label>
            <label>品牌<input name="brand" /></label>
            <label>商品名<input name="name" /></label>
            <label>补货线<input name="reorder_point" type="number" min="0" /></label>
            <label>备注<input name="note" placeholder="本次变动原因" /></label>
            <button className="secondary-button" type="submit">调整库存</button>
          </form>
        ) : null}
        <div className="table-wrap"><table>
          <thead><tr><th>SKU</th><th>仓库</th><th>现存</th><th>预留</th><th>可用</th><th>更新时间</th></tr></thead>
          <tbody>{inventory.map((item) => (
            <tr key={`${item.sku}-${item.warehouse ?? "default"}`}><td><strong>{item.sku}</strong></td><td>{item.warehouse || "默认仓"}</td><td>{item.on_hand}</td><td>{item.reserved ?? 0}</td><td><strong>{item.available ?? item.on_hand - (item.reserved ?? 0)}</strong></td><td>{formatMaybeTime(item.updated_at)}</td></tr>
          ))}</tbody>
        </table></div>
        {!inventory.length ? <Empty text="暂无库存记录" /> : null}
      </section>
    </div>
  );
}

function OrderDetail({ order, canOperate, onPayment, onShipment, onShipmentStatus, onStatusChange }: {
  order: Order;
  canOperate: boolean;
  onPayment: (event: FormEvent<HTMLFormElement>) => void;
  onShipment: (event: FormEvent<HTMLFormElement>) => void;
  onShipmentStatus: (shipmentId: number, status: "in_transit" | "delivered" | "exception") => void;
  onStatusChange: (status: "confirmed" | "cancelled" | "completed") => void;
}) {
  return (
    <div>
      <div className="detail-title"><div><p className="eyebrow">订单 #{order.id}</p><h2>{order.order_number ?? `订单 #${order.id}`}</h2></div><Badge value={order.status} /></div>
      {canOperate && order.status === "draft" ? <div className="action-row"><button className="primary-button" type="button" onClick={() => onStatusChange("confirmed")}>确认并预留库存</button><button className="secondary-button" type="button" onClick={() => onStatusChange("cancelled")}>取消订单</button></div> : null}
      {canOperate && order.status === "confirmed" ? <div className="action-row"><button className="secondary-button" type="button" onClick={() => onStatusChange("cancelled")}>取消并释放库存</button></div> : null}
      <div className="order-summary">
        <span>客户<strong>{order.customer_name ?? `#${order.customer_id}`}</strong></span>
        <span>总额<strong>{order.currency} {order.total_amount}</strong></span>
        <span>付款<strong>{statusLabel[order.payment_status ?? ""] ?? order.payment_status ?? "待记录"}</strong></span>
        <span>履约<strong>{statusLabel[order.fulfillment_status ?? ""] ?? order.fulfillment_status ?? "待处理"}</strong></span>
      </div>
      {order.shipping_address ? <p className="shipping-address"><strong>收货地址：</strong>{order.shipping_address}</p> : null}
      <h3>订单明细</h3>
      <div className="table-wrap"><table>
        <thead><tr><th>SKU</th><th>数量</th><th>单价</th><th>预留</th><th>已履约</th></tr></thead>
        <tbody>{(order.items ?? []).map((item, index) => <tr key={item.id ?? `${item.sku}-${index}`}><td><strong>{item.sku}</strong><small>{item.brand || item.description || ""}</small></td><td>{item.quantity}</td><td>{order.currency} {item.unit_price}</td><td>{item.reserved_quantity ?? 0}</td><td>{item.fulfilled_quantity ?? 0}</td></tr>)}</tbody>
      </table></div>

      <div className="section-divider" />
      <div className="order-subgrid">
        <section>
          <h3>付款记录</h3>
          <div className="timeline-list">{(order.payments ?? []).map((payment) => <article key={payment.id}><div><strong>{payment.currency ?? order.currency} {payment.amount}</strong><Badge value={payment.status} /></div><small>{payment.method}{payment.transaction_reference || payment.reference ? ` · ${payment.transaction_reference ?? payment.reference}` : ""} · {formatMaybeTime(payment.paid_at ?? payment.created_at)}</small></article>)}{!order.payments?.length ? <Empty text="暂无付款" /> : null}</div>
          {canOperate ? <form className="mini-form" onSubmit={onPayment}><label>金额<input name="amount" type="number" min="0.01" step="0.01" required /></label><label>币种<input name="currency" defaultValue={order.currency} /></label><label>方式<select name="method"><option value="bank_transfer">银行转账</option><option value="cash">现金</option><option value="other">其他</option></select></label><label>状态<select name="status"><option value="paid">已支付</option><option value="pending">待支付</option><option value="failed">失败</option><option value="refunded">已退款</option></select></label><label>流水号<input name="transaction_reference" /></label><button className="secondary-button" type="submit">记录付款</button></form> : null}
        </section>
        <section>
          <h3>物流记录</h3>
          <div className="timeline-list">{(order.shipments ?? []).map((shipment) => <article key={shipment.id}><div><strong>{shipment.carrier}</strong><Badge value={shipment.status} /></div><small>{shipment.tracking_number || "待生成运单号"} · {formatMaybeTime(shipment.shipped_at ?? shipment.created_at)}</small>{canOperate && shipment.status !== "delivered" ? <div className="action-row"><button className="link-button" type="button" onClick={() => onShipmentStatus(shipment.id, "in_transit")}>运输中</button><button className="link-button" type="button" onClick={() => onShipmentStatus(shipment.id, "delivered")}>确认送达</button><button className="link-button" type="button" onClick={() => onShipmentStatus(shipment.id, "exception")}>异常</button></div> : null}</article>)}{!order.shipments?.length ? <Empty text="暂无物流" /> : null}</div>
          {canOperate ? <form className="mini-form" onSubmit={onShipment}><label>承运商<input name="carrier" required /></label><label>运单号<input name="tracking_number" /></label><label>状态<select name="status"><option value="shipped">已发货</option><option value="pending">待发货</option><option value="in_transit">运输中</option><option value="delivered">已送达</option></select></label><button className="secondary-button" type="submit">记录物流</button></form> : null}
        </section>
      </div>
    </div>
  );
}

function sectionTitle(section: Section) {
  return {
    dashboard: "业务总览",
    inquiries: "询价中心",
    tasks: "运营待办",
    customers: "客户与品牌",
    quotes: "报价记录",
    distributions: "分发任务",
    orders: "订单与履约",
    audit: "审计日志",
    users: "用户管理",
  }[section];
}

function roleLabel(role: string) {
  return { admin: "管理员", operator: "运营", viewer: "只读" }[role] ?? role;
}

function DashboardView({ dashboard, inquiries, tasks, onOpen }: { dashboard: Dashboard; inquiries: Inquiry[]; tasks: OpsTask[]; onOpen: (value: Inquiry) => void }) {
  const cards = [
    ["询价总数", dashboard.inquiries_total, "all"],
    ["需要关注", dashboard.inquiries_need_attention, "attention"],
    ["已生成报价", dashboard.quotes_total, "quote"],
    ["开放待办", dashboard.open_tasks, "task"],
    ["待发分发", dashboard.pending_distributions, "send"],
  ];
  return (
    <>
      <section className="metric-grid">{cards.map(([label, value, tone]) => <article className={`metric-card tone-${tone}`} key={label}><span>{label}</span><strong>{value}</strong></article>)}</section>
      <div className="dashboard-grid">
        <section className="panel">
          <div className="panel-head"><div><h2>最近询价</h2><p>优先处理缺失与异常记录</p></div></div>
          <div className="compact-list">{inquiries.slice(0, 6).map((item) => <button key={item.id} onClick={() => onOpen(item)}><span><strong>{item.brand || "待识别品牌"}</strong><small>{item.sender_name}</small></span><Badge value={item.status} /></button>)}{!inquiries.length ? <Empty text="暂无询价" /> : null}</div>
        </section>
        <section className="panel signal-panel">
          <div className="panel-head"><div><h2>运营信号</h2><p>需要人工判断的事项</p></div></div>
          <div className="signal-number">{tasks.filter((task) => task.status !== "resolved").length || dashboard.open_tasks}</div>
          <p>条开放待办</p><div className="progress"><span style={{ width: `${Math.min(100, dashboard.open_tasks * 12)}%` }} /></div>
          <small>缺失字段、低置信度和异常条款会自动汇总到这里。</small>
        </section>
      </div>
    </>
  );
}

function CreateInquiryForm({ onSubmit }: { onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return (
    <div className="create-state"><p className="eyebrow">快速录入</p><h2>新增上游消息</h2><p>系统会自动抽取品牌、SKU、采购价、币种和 MOQ，并把缺失项转为运营待办。</p><form className="stack-form" onSubmit={onSubmit}><label>发送方<input name="sender_name" placeholder="示例供应商" required /></label><label>消息原文<textarea name="content" rows={9} placeholder="品牌: Nike; SKU: DD123; 采购价: 100 USD; MOQ: 20; 交货地: 上海" required /></label><button className="primary-button" type="submit">抽取并创建询价</button></form></div>
  );
}

function InquiryDetail({ inquiry, matches, quote, canOperate, downloading, onSave, onQuote, onDownload, onDistribute }: {
  inquiry: Inquiry;
  matches: CustomerMatch[];
  quote?: Quote;
  canOperate: boolean;
  downloading: string | null;
  onSave: (event: FormEvent<HTMLFormElement>) => void;
  onQuote: (event: FormEvent<HTMLFormElement>) => void;
  onDownload: (quote: Quote, format: "xlsx" | "pdf") => void;
  onDistribute: (customerId: number) => void;
}) {
  return (
    <div>
      <div className="detail-title"><div><p className="eyebrow">询价 #{inquiry.id}</p><h2>{inquiry.brand || "待补全询价"}</h2></div><Badge value={inquiry.status} /></div>
      <div className="source-box"><span>原始消息 · {inquiry.source_channel}</span><p>{inquiry.source_content}</p><small>{inquiry.sender_name} · 抽取来源 {inquiry.extraction_source} · 置信度 {Math.round(Number(inquiry.confidence) * 100)}%</small></div>
      {inquiry.missing_fields.length ? <div className="missing-box"><strong>需要补充</strong><div className="tag-row">{inquiry.missing_fields.map((field) => <span className="tag warning" key={field}>{fieldLabel[field] || field}</span>)}</div></div> : null}
      <form className="field-grid" onSubmit={onSave} key={`${inquiry.id}-${inquiry.updated_at}`}>
        <label>品牌<input name="brand" defaultValue={inquiry.brand || ""} disabled={!canOperate} /></label><label>SKU/货号<input name="sku" defaultValue={inquiry.sku || ""} disabled={!canOperate} /></label>
        <label>行业<input name="industry" defaultValue={inquiry.industry || ""} disabled={!canOperate} /></label><label>目标区域<input name="target_region" defaultValue={inquiry.target_region || ""} disabled={!canOperate} /></label>
        <label>采购价<input name="purchase_price" type="number" step="0.01" defaultValue={inquiry.purchase_price || ""} disabled={!canOperate} /></label><label>币种<input name="currency" defaultValue={inquiry.currency || ""} disabled={!canOperate} /></label>
        <label>MOQ<input name="moq" type="number" defaultValue={inquiry.moq || ""} disabled={!canOperate} /></label><label>交货地<input name="delivery_location" defaultValue={inquiry.delivery_location || ""} disabled={!canOperate} /></label>
        <label>贸易条款<select name="trade_term" defaultValue={inquiry.trade_term} disabled={!canOperate}><option>EXW</option><option>FOB</option><option>CIF</option><option>DAP</option></select></label><label>库存类型<select name="stock_type" defaultValue={inquiry.stock_type} disabled={!canOperate}><option>现货</option><option>期货</option></select></label>
        <label className="span-two">付款条款<input name="payment_terms" defaultValue={inquiry.payment_terms} disabled={!canOperate} /></label><label className="span-two">发货周期<input name="lead_time" defaultValue={inquiry.lead_time || ""} disabled={!canOperate} /></label>
        {canOperate ? <button className="secondary-button span-two" type="submit">保存并重新校验</button> : null}
      </form>
      {canOperate ? <><div className="section-divider" /><h3>生成脱敏报价</h3><form className="inline-form" onSubmit={onQuote}><label>场景<select name="scenario"><option value="A">A · 非天猫 5%</option><option value="B">B · 天猫 15%</option></select></label><label>输出币种<input name="output_currency" defaultValue="CNY" /></label><label>汇率<input name="fx_rate" type="number" step="0.0001" placeholder="留空使用最新汇率" /></label><label>单件运费<input name="freight_per_unit" type="number" min="0" step="0.01" defaultValue="0" /></label><button className="primary-button" type="submit" disabled={inquiry.status === "need_info"}>生成报价</button></form></> : null}
      {quote ? <div className="quote-preview"><div><span>最新报价 v{quote.version}</span><strong>{quote.output_currency} {quote.unit_price}</strong></div><div className="quote-preview-actions"><span className={quote.sensitive_scan_passed ? "safe" : "danger"}>{quote.sensitive_scan_passed ? "敏感字段检查通过" : "敏感字段检查未通过"}</span><button disabled={downloading !== null || !quote.sensitive_scan_passed} onClick={() => onDownload(quote, "xlsx")}>Excel</button><button disabled={downloading !== null || !quote.sensitive_scan_passed} onClick={() => onDownload(quote, "pdf")}>PDF</button></div></div> : null}
      <div className="section-divider" /><h3>匹配客户</h3>
      <div className="match-list">{matches.map((match) => <article key={match.customer.id}><div><strong>{match.customer.name}</strong><small>{matchSummary(match)} · 匹配度 {match.score}%</small>{match.reasons && Object.keys(match.reasons).length ? <small>{Object.entries(match.reasons).map(([field, score]) => `${matchFieldLabel(field)} +${score}`).join("；")}</small> : null}</div>{canOperate ? <button className="link-button" disabled={!quote} onClick={() => onDistribute(match.customer.id)}>确认分发</button> : null}</article>)}{!matches.length ? <Empty text="没有达到阈值的匹配客户" /> : null}</div>
    </div>
  );
}

function matchSummary(match: CustomerMatch) {
  return (match.matched_fields ?? []).map(matchFieldLabel).join(" · ") || match.matched_brand || "综合匹配";
}

function matchFieldLabel(field: string) {
  return { brand: "品牌", sku: "SKU", industry: "行业", region: "区域" }[field] ?? field;
}

function Empty({ text }: { text: string }) { return <div className="empty">{text}</div>; }

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

function formatMaybeTime(value?: string | null) { return value ? formatTime(value) : "—"; }

export default App;
