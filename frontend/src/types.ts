export type User = {
  id?: number;
  username: string;
  name?: string;
  display_name?: string;
  role: "admin" | "operator" | "viewer" | string;
  active?: boolean;
  created_at?: string;
  last_login_at?: string | null;
};

export type AuthSession = {
  access_token: string;
  token_type: string;
  user: User;
};

export type Dashboard = {
  inquiries_total: number;
  inquiries_need_attention: number;
  quotes_total: number;
  open_tasks: number;
  pending_distributions: number;
};

export type Inquiry = {
  id: number;
  status: string;
  brand: string | null;
  sku: string | null;
  industry: string | null;
  target_region: string | null;
  purchase_price: string | null;
  currency: string | null;
  moq: number | null;
  delivery_location: string | null;
  trade_term: string;
  payment_terms: string;
  lead_time: string | null;
  stock_type: string;
  confidence: string;
  missing_fields: string[];
  extraction_source: string;
  created_at: string;
  updated_at: string;
  source_channel: string;
  sender_name: string;
  sender_contact: string | null;
  source_content: string;
};

export type OpsTask = {
  id: number;
  inquiry_id: number;
  task_type: string;
  status: string;
  priority: string;
  title: string;
  detail: Record<string, unknown>;
  suggested_reply: string | null;
  assignee: string | null;
  created_at: string;
};

export type Quote = {
  id: number;
  inquiry_id: number;
  version: number;
  scenario: string;
  output_currency: string;
  fx_rate: string;
  freight: string;
  margin_rate: string;
  unit_price: string;
  pricing_snapshot: Record<string, string>;
  downstream_snapshot: Record<string, unknown>;
  sensitive_scan_passed: boolean;
  created_by: string;
  created_at: string;
};

export type Distribution = {
  id: number;
  quote_id: number;
  customer_id: number;
  customer_name: string;
  channel: string;
  status: string;
  confirmed_by: string | null;
  attempt_count: number;
  max_attempts: number;
  provider_message_id: string | null;
  error_message: string | null;
  sent_at: string | null;
  delivered_at: string | null;
  created_at: string;
};

export type Customer = {
  id: number;
  name: string;
  email: string | null;
  channel: string;
  channel_target: string | null;
  active: boolean;
  brands: string[];
  industries?: string[];
  regions?: string[];
  sku_patterns?: string[];
  match_threshold?: number;
};

export type CustomerMatch = {
  customer: Customer;
  matched_brand?: string | null;
  score: number;
  matched_fields?: string[];
  reasons?: Record<string, number>;
};

export type AuditLog = {
  id: number;
  entity_type: string;
  entity_id: string;
  action: string;
  actor: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type OrderItem = {
  id?: number;
  sku: string;
  brand?: string | null;
  description?: string | null;
  quantity: number;
  unit_price: string | number;
  reserved_quantity?: number;
  fulfilled_quantity?: number;
};

export type Payment = {
  id: number;
  amount: string | number;
  currency?: string;
  method: string;
  status: string;
  reference?: string | null;
  transaction_reference?: string | null;
  paid_at?: string | null;
  created_at?: string;
};

export type Shipment = {
  id: number;
  carrier: string;
  tracking_number?: string | null;
  status: string;
  quantity?: number;
  shipped_at?: string | null;
  delivered_at?: string | null;
  created_at?: string;
  events?: Array<{ id: number; status: string; location?: string | null; description?: string | null; occurred_at: string }>;
};

export type Order = {
  id: number;
  order_number?: string;
  customer_id: number;
  customer_name?: string;
  quote_id?: number | null;
  status: string;
  currency: string;
  total_amount: string | number;
  payment_status?: string;
  fulfillment_status?: string;
  shipping_address?: string | null;
  notes?: string | null;
  items: OrderItem[];
  payments?: Payment[];
  shipments?: Shipment[];
  created_at: string;
  updated_at?: string;
};

export type InventoryItem = {
  id?: number;
  sku: string;
  warehouse?: string | null;
  on_hand: number;
  reserved?: number;
  available?: number;
  updated_at?: string;
};
