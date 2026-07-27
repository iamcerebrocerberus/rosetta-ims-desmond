import { C } from '@/lib/tokens'
import { createFileRoute } from '@tanstack/react-router'
import { useState, useEffect, useCallback, useRef, useMemo, type CSSProperties } from 'react'
import { Spinner } from '@/components/Spinner'
import { authHeaders, getUser, can } from '@/lib/auth'
import { skuToPath } from '@/lib/sku'
import { toast } from '@/lib/toast'
import { confirmDialog } from '@/lib/confirm'
import { ReparseButton } from '@/components/ReparseButton'
import { API_BASE } from '@/lib/config'

const API = API_BASE

// IMS operational item categories — the only category list the picker offers.
const CATEGORIES = [
  'Medicine', 'Preventative', 'Supplement', 'Shampoo', 'Food',
  'Not-For-Sale', 'Pet Hygiene', 'Cat Litter', 'Others',
]
// Item category → SKU leading digit (mirrors backend ITEM_CATEGORY_DIGIT).
const SKU_CATEGORY_DIGIT: Record<string, string> = {
  'Food': '1', 'Medicine': '5', 'Preventative': '5', 'Supplement': '5',
  'Shampoo': '4', 'Pet Hygiene': '4', 'Cat Litter': '4',
  'Not-For-Sale': '6', 'Others': '7',
}

// Onboarding audit trail — who did what to a catalogue item / SKU.
interface AuditEvent {
  id: number
  item_id: number | null
  import_id: number | null
  product_id: number | null
  sku_code: string | null
  action: string            // confirm_match | assign_new | edit | reject | supplier_confirm
  user_id: number | null
  username: string | null
  display_name: string | null
  details: Record<string, unknown>
  created_at: string
}
const ACTION_BADGE: Record<string, { label: string; bg: string; color: string }> = {
  confirm_match:    { label: 'Matched',   bg: C.greenBg, color: C.green },
  assign_new:       { label: 'New SKU',   bg: '#DBEAFE', color: '#1E40AF' },
  edit:             { label: 'Edited',    bg: C.warnBg, color: C.amberInk },
  reject:           { label: 'Rejected',  bg: C.redBg, color: C.redInk },
  supplier_confirm: { label: 'Supplier',  bg: '#EDE9FE', color: '#5B21B6' },
  skip_verified:    { label: 'Already-verified', bg: '#FFEDD5', color: '#9A3412' },
}
function fmtWhen(iso: string): string {
  // Stored as naive UTC ISO (no 'Z'); render in the viewer's locale.
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z')
  return isNaN(d.getTime()) ? iso : d.toLocaleString()
}
// Weight is canonical in grams; kg/lb is just the display/source unit.
const LB_G = 453.592
const gToUnit = (g: number, u: string | null) => +(g / (u === 'lb' ? LB_G : 1000)).toFixed(3)
const unitToG = (v: number, u: string | null) => Math.round(v * (u === 'lb' ? LB_G : 1000))
const fmtWeight = (g: number | null | undefined, u: string | null) => g == null ? '' : `${gToUnit(g, u)} ${u || 'kg'}`
// Shared cell styles for the compact Skipped / Confirmed tables.
const thCell: CSSProperties = { padding: '9px 14px', fontSize: '11px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.03em', color: C.muted }
const tdCell: CSSProperties = { padding: '10px 14px', verticalAlign: 'top' }
// Shared filter bar for the Skipped / Confirmed tables: free-text search + supplier + reviewer
// dropdowns, driven by server-side facets so they cover the whole list, not just the loaded page.
function OnboardFilterBar(props: {
  search: string; setSearch: (v: string) => void
  supplier: string; setSupplier: (v: string) => void
  supplierFacets: { supplier_id: number | null; count: number }[]
  user: string; setUser: (v: string) => void
  userFacets: { user: string; count: number }[]
  userLabel: string
  suppliers: { id: number; name: string }[]
}) {
  const { search, setSearch, supplier, setSupplier, supplierFacets, user, setUser, userFacets, userLabel, suppliers } = props
  const sel: CSSProperties = { border: '1px solid #E2E8F0', borderRadius: '8px', padding: '8px 10px', fontSize: '12.5px', background: 'white', color: C.ink }
  return (
    <div style={{ display: 'flex', gap: '8px', marginBottom: '12px', flexWrap: 'wrap', alignItems: 'center' }}>
      <div style={{ position: 'relative', flex: '1 1 220px', minWidth: '180px' }}>
        <span style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', fontSize: '12px', color: C.faint, pointerEvents: 'none' }}>🔍</span>
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search SKU, description, brand…"
          style={{ width: '100%', border: '1px solid #E2E8F0', borderRadius: '8px', padding: '8px 28px', fontSize: '12.5px', background: 'white', color: C.ink }} />
        {search && <button onClick={() => setSearch('')} title="Clear"
          style={{ position: 'absolute', right: '8px', top: '50%', transform: 'translateY(-50%)', border: 'none', background: C.monoBg, color: C.muted, borderRadius: '50%', width: '18px', height: '18px', fontSize: '11px', cursor: 'pointer', lineHeight: 1 }}>×</button>}
      </div>
      <select value={supplier} onChange={e => setSupplier(e.target.value)} style={sel}>
        <option value="">All suppliers</option>
        {supplierFacets.filter(f => f.supplier_id != null).map(f =>
          <option key={f.supplier_id} value={String(f.supplier_id)}>{suppliers.find(s => s.id === f.supplier_id)?.name ?? `#${f.supplier_id}`} ({f.count})</option>)}
      </select>
      <select value={user} onChange={e => setUser(e.target.value)} style={sel}>
        <option value="">{userLabel}</option>
        {userFacets.map(f => <option key={f.user} value={f.user}>{f.user} ({f.count})</option>)}
      </select>
    </div>
  )
}
function auditSummary(e: AuditEvent): string {
  const d = e.details ?? {}
  const s = (k: string) => (d[k] == null ? '' : String(d[k]))
  switch (e.action) {
    case 'assign_new':    return [s('product_name'), s('category')].filter(Boolean).join(' · ')
    case 'confirm_match': return s('product_name') || ''
    case 'reject':        return s('reason') || s('description') || '—'
    case 'supplier_confirm': return [s('supplier_name'), s('filename')].filter(Boolean).join(' · ')
    case 'edit': {
      const ch = (d.changes ?? {}) as Record<string, { from: unknown; to: unknown }>
      const keys = Object.keys(ch)
      if (!keys.length) return 'edited'
      return keys.map(k => `${k}: ${ch[k].from ?? '∅'}→${ch[k].to ?? '∅'}`).join(', ')
    }
    default: return ''
  }
}

interface Supplier { id: number; code: string; name: string }
interface SuggestedMatch {
  tags?: string[]              // top match only: the product's REAL tags (shopify-sourced when available)
  tags_source?: string | null  // 'shopify' | 'mixed' | null
  sku_code: string
  name: string
  match_type: string
  confidence: number
  brand: string | null
  status: string | null
  units_per_pack: number | null
  basic_cost: number | null
  uom: string | null
}

interface FieldDiff {
  ok: boolean
  label: string
  detail?: string
}
interface DiffResult {
  fields: { name: FieldDiff; brand: FieldDiff; pack: FieldDiff; cost: FieldDiff }
  diff_count: number
  match_grade: 'perfect' | 'partial' | 'weak'   // visual frame color
}

// Adapt a Find-&-Match search hit into a match the panel can render. match_type 'manual'
// signals the reviewer explicitly chose this SKU (so it isn't shown as an AI confidence %).
function asPickedMatch(r: SkuResult): SuggestedMatch {
  return {
    sku_code: r.sku_code, name: r.name, brand: r.brand, status: r.status,
    units_per_pack: r.units_per_pack, uom: r.uom,
    basic_cost: r.basic_cost ?? r.primary_cost,
    match_type: 'manual', confidence: 1,
  }
}

function computeDiff(item: QueueItem, m: SuggestedMatch): DiffResult {
  // name: confidence directly (already 0-1); a manual pick is human-chosen, so always OK
  const manual = m.match_type === 'manual'
  const nameOk = manual || m.confidence >= 0.85
  const nameField: FieldDiff = {
    ok: nameOk,
    label: manual ? 'manual pick' : `${Math.round(m.confidence * 100)}%`,
  }

  // brand
  const ib = (item.brand ?? '').toLowerCase().trim()
  const mb = (m.brand ?? '').toLowerCase().trim()
  const brandOk = (!ib && !mb) || (ib !== '' && mb !== '' && ib === mb)
  const brandField: FieldDiff = {
    ok: brandOk,
    label: brandOk ? 'same' : (mb ? 'different' : 'unknown'),
  }

  // pack
  const packOk = item.units_per_pack != null && m.units_per_pack != null
    ? item.units_per_pack === m.units_per_pack
    : item.units_per_pack == null && m.units_per_pack == null
  const packField: FieldDiff = {
    ok: packOk,
    label: packOk ? 'same' : (m.units_per_pack ? `IMS ${m.units_per_pack}` : 'unknown'),
  }

  // cost: % diff
  let costField: FieldDiff
  if (item.cost_price != null && m.basic_cost != null && m.basic_cost > 0) {
    const pct = (item.cost_price - m.basic_cost) / m.basic_cost * 100
    const abs = Math.abs(pct)
    costField = {
      ok: abs <= 10,
      label: abs <= 1 ? '±0%' : `${pct > 0 ? '+' : ''}${pct.toFixed(0)}%`,
      detail: `extracted $${item.cost_price.toFixed(0)} vs IMS $${m.basic_cost.toFixed(0)}`,
    }
  } else {
    costField = { ok: false, label: 'unknown', detail: 'no IMS cost on this supplier' }
  }

  const fields = { name: nameField, brand: brandField, pack: packField, cost: costField }
  const diff_count = Object.values(fields).filter(f => !f.ok).length
  const match_grade: DiffResult['match_grade'] =
    diff_count === 0 ? 'perfect' :
    diff_count <= 1 ? 'partial' : 'weak'

  return { fields, diff_count, match_grade }
}
interface QueueItem {
  id: number
  import_id: number
  supplier_id: number | null
  raw_description: string | null
  original_description: string | null
  supplier_sku: string | null
  barcode: string | null
  cost_price: number | null
  uom: string | null
  units_per_pack: number | null
  min_sellable_qty: number | null
  brand: string | null
  variant: string | null
  pack_size: string | null
  bulk_buy_tiers: string | null
  max_bulk_buy_cost: number | null
  max_bulk_buy_min_qty: number | null
  confidence_score: number
  review_status: string
  skipped: boolean
  suggested_matches: SuggestedMatch[]
  import_filename: string | null
  ai_tags: string[]
  ai_category: string | null
  ai_subcategory: string | null
  species: string | null
  weight_grams: number | null
  weight_unit: string | null
  rrp: number | null
  min_purchase_qty: number | null
}

// A row in the Confirmed list — an item already matched to an existing SKU or assigned a new one.
interface ConfirmedItem {
  id: number
  raw_description: string | null
  original_description: string | null
  action: string                 // 'matched' | 'new_sku'
  sku: string | null             // resulting inventory SKU (link target)
  product_name: string | null
  supplier_name: string | null
  reviewed_by: string | null
  reviewed_at: string | null
}

// A pending item whose top match is a SKU already HITL-verified (a re-upload duplicate).
interface AlreadyVerifiedItem {
  id: number
  raw_description: string | null
  matched_sku: string
  matched_name: string | null
  match_type: string
  confidence: number
}

// One day of onboarding throughput for the Daily report tab.
interface DailyRow {
  date: string
  matched: number
  new_sku: number
  rejected: number
  skipped: number
  processed: number          // matched + new_sku + rejected
  total: number              // processed + skipped
  reviewers: string[]
  reviewer_count: number
}
interface DailyReport {
  days_requested: number
  from: string
  totals: { matched: number; new_sku: number; rejected: number; skipped: number; processed: number; active_days: number }
  days: DailyRow[]
}

// A single inventory SKU returned by the Find-&-Match search picker.
interface SkuResult {
  sku_code: string
  name: string
  brand: string | null
  basic_cost: number | null
  primary_cost: number | null
  uom: string | null
  units_per_pack: number | null
  status: string
}

// Triage tiers — see comment block in TierLanding for explanation.
// Granular confidence tiers for task delegation:
//   t1a = 99%+ (exact match, auto-approve safe)
//   t1b = 95-98% (very high, quick eyeball)
//   t2a = 85-94% (good match, verify fields)
//   t2b = 65-84% (possible, needs careful review)
//   t3  = no match, brand NOT in IMS (likely reject)
//   t4  = no match, brand IS in IMS (find & match or new SKU)
type TierId = 't1a' | 't1b' | 't2a' | 't2b' | 't3' | 't4'

function classifyTier(item: QueueItem, brandsInDb: Set<string>): TierId {
  const topMatch = item.suggested_matches?.[0]
  if (topMatch && topMatch.confidence >= 0.99) return 't1a'
  if (topMatch && topMatch.confidence >= 0.95) return 't1b'
  if (topMatch && topMatch.confidence >= 0.85) return 't2a'
  if (topMatch && topMatch.confidence >= 0.65) return 't2b'
  const itemBrand = (item.brand ?? '').toLowerCase().trim()
  if (itemBrand && brandsInDb.has(itemBrand)) return 't4'
  return 't3'
}
interface ScanLogEntry {
  import_id: number
  supplier_id: number | null
  supplier_name: string | null
  filename: string
  format: string | null
  imported_at: string
  real_items: number
  error_items: number
  status: 'ok' | 'error' | 'empty'
}
interface ScanLogData {
  total_imports: number
  total_items: number
  total_errors: number
  successful: number
  failed: number
  log: ScanLogEntry[]
}

interface CatalogueImport {
  id: number
  supplier_id: number | null
  supplier_name: string | null
  supplier_segment: string | null
  filename: string
  format: string
  imported_at: string
  status: string
  item_count: number
  counts: { pending: number; matched: number; new_sku: number; rejected: number }
  // supplier detection / resolution (stage-1 confirm)
  detected_supplier_name: string | null
  detected_brands: string | null
  supplier_confidence: number | null
  supplier_source: string | null
  supplier_status: string | null   // 'confirmed' | 'needs_review'
}

type ActionMode = 'idle' | 'match_select' | 'match_manual' | 'new_sku' | 'reject' | 'edit'

// Editable fields the reviewer can correct before approving (mirror of backend ItemEdit).
// Kept as strings so number inputs round-trip cleanly; parsed on save.
interface EditDraft {
  raw_description: string
  brand: string
  variant: string
  supplier_sku: string
  barcode: string
  cost_price: string
  uom: string
  units_per_pack: string
  min_sellable_qty: string
  bulk_buy_tiers: string
  species: string
  weight_value: string   // weight in the selected unit (converted to grams on save)
  weight_unit: string    // 'kg' | 'lb'
  rrp: string
  min_purchase_qty: string
  pack_size: string
  max_bulk_buy_cost: string
  max_bulk_buy_min_qty: string
  supplier_id: string          // supplier re-assignment (id as string for the <select>)
}
interface ItemAction {
  mode: ActionMode
  manualSku: string
  category: string
  name: string
  brand: string
  matchName: string       // optional rename of the MATCHED product's title ('' = keep as is)
  rejectReason: string
  edit: EditDraft | null
  tags: string[] | null   // null = not yet edited (fall back to item.ai_tags)
  tagInput: string
  subcategory: string | null   // null = not edited (fall back to item.ai_subcategory)
  pickedMatch: SuggestedMatch | null  // manually-chosen match (Find & Match) — overrides the AI suggestion until confirmed
}

function defaultAction(): ItemAction {
  return { mode: 'idle', manualSku: '', category: '', name: '', brand: '', matchName: '', rejectReason: 'clinical_consumable', edit: null, tags: null, tagInput: '', subcategory: null, pickedMatch: null }
}

// Effective tag list for an item: the reviewer's edits if any, else the AI suggestions.
function effectiveTags(a: ItemAction, item: QueueItem): string[] {
  return a.tags ?? item.ai_tags ?? []
}
// Effective category: the reviewer's pick if any, else the AI suggestion, else Food.
function effectiveCategory(a: ItemAction, item: QueueItem): string {
  return a.category || item.ai_category || 'Food'
}
// Effective subcategory: reviewer's edit if any, else AI suggestion.
function effectiveSubcategory(a: ItemAction, item: QueueItem): string {
  return a.subcategory ?? item.ai_subcategory ?? ''
}
function normTag(s: string): string {
  return s.trim().toLowerCase().replace(/\s+/g, ' ')
}

function seedEdit(item: QueueItem): EditDraft {
  return {
    raw_description: item.raw_description ?? '',
    brand:          item.brand ?? '',
    variant:        item.variant ?? '',
    supplier_sku:   item.supplier_sku ?? '',
    barcode:        item.barcode ?? '',
    cost_price:     item.cost_price != null ? String(item.cost_price) : '',
    uom:            item.uom ?? '',
    units_per_pack: item.units_per_pack != null ? String(item.units_per_pack) : '',
    min_sellable_qty: item.min_sellable_qty != null ? String(item.min_sellable_qty) : '',
    bulk_buy_tiers: item.bulk_buy_tiers ?? '',
    species:        item.species ?? '',
    weight_value:   item.weight_grams != null ? String(gToUnit(item.weight_grams, item.weight_unit)) : '',
    weight_unit:    item.weight_unit ?? 'kg',
    rrp:            item.rrp != null ? String(item.rrp) : '',
    min_purchase_qty: item.min_purchase_qty != null ? String(item.min_purchase_qty) : '',
    pack_size:      item.pack_size ?? '',
    max_bulk_buy_cost: item.max_bulk_buy_cost != null ? String(item.max_bulk_buy_cost) : '',
    max_bulk_buy_min_qty: item.max_bulk_buy_min_qty != null ? String(item.max_bulk_buy_min_qty) : '',
    supplier_id:    item.supplier_id != null ? String(item.supplier_id) : '',
  }
}

function MatchPill({ type }: { type: string }) {
  const map: Record<string, string> = { barcode: C.indigo, supplier_sku: '#8B5CF6', name_fuzzy: C.muted }
  const label: Record<string, string> = { barcode: 'barcode', supplier_sku: 'SKU', name_fuzzy: 'name' }
  const color = map[type] ?? C.faint
  return <span style={{ fontSize: '10px', color, fontWeight: 600 }}>{label[type] ?? type}</span>
}

function Btn({ onClick, disabled, loading, bg, color, children, loadingLabel }: {
  onClick: () => void; disabled?: boolean; loading?: boolean; bg: string; color: string
  children: React.ReactNode; loadingLabel?: React.ReactNode
}) {
  const off = disabled || loading
  return (
    <button
      onClick={onClick}
      disabled={off}
      aria-busy={loading || undefined}
      style={{
        background: off ? C.monoBg : bg,
        color: off ? (loading ? C.muted : C.knobOff) : color,
        border: 'none', borderRadius: '5px', padding: '5px 12px',
        fontSize: '12px', fontWeight: 600,
        cursor: off ? 'default' : 'pointer',
        whiteSpace: 'nowrap',
        display: 'inline-flex', alignItems: 'center', gap: '6px',
        transition: 'background 0.12s, color 0.12s',
      }}
    >
      {loading && <Spinner color={C.muted} />}
      {loading ? (loadingLabel ?? children) : children}
    </button>
  )
}

function Ghost({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button onClick={onClick} style={{ background: 'none', border: 'none', fontSize: '12px', color: C.faint, cursor: 'pointer', padding: '5px 6px' }}>
      {children}
    </button>
  )
}

// Controlled vocabulary for the sell-unit (UOM) dropdown — what ONE sellable unit is.
const UOM_OPTIONS = [
  'tablet', 'capsule', 'sachet', 'packet', 'bottle', 'vial', 'ampoule', 'tube',
  'strip', 'ml', 'L', 'g', 'kg', 'can', 'pouch', 'bag', 'box', 'pump', 'dose',
  'syringe', 'pipette', 'each',
]

function EditField({ label, value, onChange, type = 'text', wide = false, list, options }: {
  label: string; value: string; onChange: (v: string) => void; type?: string; wide?: boolean; list?: string; options?: string[]
}) {
  const inputStyle = { border: '1px solid #E2E8F0', borderRadius: '5px', padding: '5px 10px', fontSize: '12px', width: '100%', boxSizing: 'border-box' as const }
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: '3px', gridColumn: wide ? '1 / -1' : 'auto' }}>
      <span style={{ fontSize: '10px', fontWeight: 600, color: C.faint }}>{label}</span>
      {options ? (
        <select value={value} onChange={e => onChange(e.target.value)} style={{ ...inputStyle, background: 'white' }}>
          <option value="">—</option>
          {/* keep a current non-standard value selectable so an OCR'd unit isn't silently lost */}
          {value && !options.includes(value) && <option value={value}>{value}</option>}
          {options.map(o => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : (
        <input type={type} value={value} list={list} onChange={e => onChange(e.target.value)} style={inputStyle} />
      )}
    </label>
  )
}

// ── Batch upload (whole-folder) ────────────────────────────────────────────────
const BATCH_EXT = new Set(['pdf', 'xlsx', 'xls', 'csv', 'jpg', 'jpeg', 'png'])
type BatchStatus = 'queued' | 'uploading' | 'done' | 'error'
interface BatchFile {
  key: string
  file: File
  name: string
  supplierFolder: string
  supplierId: number | null
  status: BatchStatus
  itemCount: number | null
  importId: number | null
  error: string | null
  // extraction result detail (shown per file)
  fmt: string | null
  detectedSupplier: string | null
  detectedBrands: string | null
  supplierStatus: string | null   // 'confirmed' | 'needs_review'
  sizeMB: number
  startedAt: number | null
}

// Path is Root / Region / Supplier / [Brand] / file — supplier is the 3rd segment.
function inferSupplierFolder(relPath: string): string {
  const parts = relPath.split('/').filter(Boolean)
  if (parts.length >= 3) return parts[2]
  if (parts.length === 2) return parts[1]
  return parts[0] ?? ''
}

function matchSupplierId(folder: string, suppliers: Supplier[]): number | null {
  const fn = folder.toLowerCase().replace('(reseller)', '').replace(' ltd', '').trim()
  if (!fn) return null
  for (const s of suppliers) {
    const name = (s.name ?? '').toLowerCase().trim()
    if (name && (name.includes(fn) || fn.includes(name))) return s.id
  }
  return null
}

const BATCH_BADGE: Record<BatchStatus, { bg: string; color: string; label: string }> = {
  queued:    { bg: C.monoBg, color: C.muted, label: 'Queued' },
  uploading: { bg: '#DBEAFE', color: '#1E40AF', label: 'Extracting…' },
  done:      { bg: C.greenBg, color: C.green, label: 'Done' },
  error:     { bg: C.redBg, color: C.redInk, label: 'Failed' },
}

// ── Batch resume across refresh ────────────────────────────────────────────
// File bytes can't be persisted (File objects aren't serializable), so we save a
// metadata-only snapshot of the batch to localStorage. After a refresh we surface a
// "resume" banner; when the user re-picks the same files we reconcile by name+size so
// already-extracted files are marked done (skipped) and only the rest re-run.
const BATCH_SNAPSHOT_KEY = 'ims_batch_snapshot'
type SerializedBatchFile = Omit<BatchFile, 'file'>
interface BatchSnapshot { savedAt: number; files: SerializedBatchFile[] }
// Stable identity that survives picker choice (folder vs files) and refresh.
const fileMatchId = (f: { name: string; sizeMB: number }) => `${f.name}|${f.sizeMB.toFixed(4)}`
// Drop the non-serializable File handle before persisting.
function serializeBatch(files: BatchFile[]): SerializedBatchFile[] {
  return files.map(f => ({
    key: f.key, name: f.name, supplierFolder: f.supplierFolder, supplierId: f.supplierId,
    status: f.status, itemCount: f.itemCount, importId: f.importId, error: f.error,
    fmt: f.fmt, detectedSupplier: f.detectedSupplier, detectedBrands: f.detectedBrands,
    supplierStatus: f.supplierStatus, sizeMB: f.sizeMB, startedAt: f.startedAt,
  }))
}

export const Route = createFileRoute('/_authed/catalogues/')({ component: CataloguesPage })

function CataloguesPage() {
  const [scanLog, setScanLog]           = useState<ScanLogData | null>(null)
  const [, setShowScanLog]   = useState(false)
  const [suppliers, setSuppliers]       = useState<Supplier[]>([])
  const [imports, setImports]           = useState<CatalogueImport[]>([])
  const [queue, setQueue]               = useState<QueueItem[]>([])
  const [pendingCount, setPendingCount] = useState(0)
  const [loading, setLoading]           = useState(true)
  const [brandsInDb, setBrandsInDb]     = useState<Set<string>>(new Set())
  const [subcatVocab, setSubcatVocab]   = useState<string[]>([])   // controlled subcategory list

  const [selectedImportId, setSelectedImportId] = useState<number | null>(null)
  const [filter, setFilter]                     = useState<'all' | 'match' | 'nomatch' | 'active' | 't1' | 't1a' | 't1b' | 't2' | 't2a' | 't2b' | 't3' | 't4'>('all')
  const [brandFilter, setBrandFilter]           = useState<string>('')   // empty = all brands
  const [supplierFilter, setSupplierFilter]     = useState<string>('')   // empty = all suppliers
  // supplier → pending-count facets for the WHOLE in-scope queue (from the server) so the
  // supplier dropdown is complete regardless of how many items the page has actually loaded.
  const [supplierFacets, setSupplierFacets] = useState<{ supplier_id: number | null; count: number }[]>([])
  const [itemSearch, setItemSearch]             = useState<string>('')   // free-text search over scanned items
  // Server-side queue search (debounced from itemSearch so it covers the WHOLE queue, not just
  // the loaded page) + the skip-bucket "who skipped" filter, with reviewer facets.
  const [queueSearch, setQueueSearch] = useState('')
  const [skippedByFilter, setSkippedByFilter] = useState('')   // empty = all reviewers (skip bucket)
  const [userFacets, setUserFacets] = useState<{ user: string; count: number }[]>([])
  // Confirmed-list filters (its own dataset + facets): supplier, reviewer, free-text search.
  const [confSupplier, setConfSupplier] = useState('')
  const [confUser, setConfUser] = useState('')
  const [confSearchInput, setConfSearchInput] = useState('')
  const [confSearch, setConfSearch] = useState('')             // debounced confSearchInput
  const [confSupplierFacets, setConfSupplierFacets] = useState<{ supplier_id: number | null; count: number }[]>([])
  const [confUserFacets, setConfUserFacets] = useState<{ user: string; count: number }[]>([])
  const [includeInactive, setIncludeInactive]   = useState(false)        // match against inactive SKUs too
  const [selectedIds, setSelectedIds]           = useState<Set<number>>(new Set())

  const [uploading] = useState(false)

  // Live preview of the next global SKU suffix (7 digits, category-independent).
  const [nextSuffix, setNextSuffix] = useState<string | null>(null)
  const flash = useCallback((msg: string) => toast.success(msg), [])
  const refreshNextSku = useCallback(async () => {
    try {
      const r = await fetch(`${API}/sku/next?category=Food`, { headers: authHeaders() })
      if (r.ok) { const d = await r.json(); if (d.next_sku) setNextSuffix(String(d.next_sku).slice(1)) }
    } catch { /* preview only — ignore */ }
  }, [])

  // Known brand list (from the supplier sheets) — for onboarding brand matching + add.
  const [knownBrands, setKnownBrands] = useState<string[]>([])
  const [brandSet, setBrandSet] = useState<Set<string>>(new Set())

  // Find & Match — live inventory search to (re)match a scan to a different SKU. Keyed by item id.
  const [skuResults, setSkuResults]     = useState<Record<number, SkuResult[]>>({})
  const [skuSearching, setSkuSearching] = useState<Record<number, boolean>>({})
  const skuTimers = useRef<Record<number, ReturnType<typeof setTimeout>>>({})
  const fetchBrands = useCallback(async () => {
    try {
      const r = await fetch(`${API}/brands`, { headers: authHeaders() })
      if (r.ok) { const d = await r.json(); setKnownBrands(d.map((b: any) => b.name)); setBrandSet(new Set(d.map((b: any) => b.normalized))) }
    } catch { /* non-critical */ }
  }, [])
  useEffect(() => { fetchBrands() }, [fetchBrands])

  // Item categories + their SKU digit, fetched live so added/edited categories flow through.
  const [catRules, setCatRules] = useState<{ category: string; sku_digit: string | null }[]>([])
  useEffect(() => {
    fetch(`${API}/category-rules`, { headers: authHeaders() }).then(r => r.ok ? r.json() : []).then(setCatRules).catch(() => {})
  }, [])
  const categoryNames = useMemo(() => (catRules.length ? catRules.map(c => c.category) : CATEGORIES), [catRules])
  const categoryDigit = useMemo(() => {
    const m: Record<string, string> = { ...SKU_CATEGORY_DIGIT }
    catRules.forEach(c => { if (c.sku_digit) m[c.category] = c.sku_digit })
    return m
  }, [catRules])
  async function addBrand(name: string, supplierId: number | null) {
    const n = name.trim()
    if (!n) return
    if (supplierId == null) { toast.error('Confirm the supplier for this file first, then add the brand.'); return }
    const r = await fetch(`${API}/brands`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ name: n, supplier_id: supplierId }),
    })
    if (r.ok) { await fetchBrands(); flash(`Brand “${n}” added to the list`) }
    else toast.error((await r.json().catch(() => ({}))).detail ?? 'Could not add brand')
  }

  // Onboarding audit trail (who confirmed/edited/new/rejected which item).
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [auditQuery, setAuditQuery] = useState('')
  const [skuHistory, setSkuHistory] = useState<{ key: string; events: AuditEvent[] } | null>(null)
  const fetchAudit = useCallback(async () => {
    try {
      const r = await fetch(`${API}/catalogues/audit?limit=80`, { headers: authHeaders() })
      if (r.ok) { const d = await r.json(); setAudit(d.events ?? []) }
    } catch { /* non-critical */ }
  }, [])

  // Daily onboarding report — per-day throughput (matched / new / rejected / skipped).
  const [daily, setDaily] = useState<DailyReport | null>(null)
  const [dailyLoading, setDailyLoading] = useState(false)
  const fetchDaily = useCallback(async () => {
    setDailyLoading(true)
    try {
      const r = await fetch(`${API}/catalogues/daily?days=30`, { headers: authHeaders() })
      if (r.ok) setDaily(await r.json())
    } catch { /* non-critical */ }
    finally { setDailyLoading(false) }
  }, [])
  const openSkuHistory = useCallback(async (sku: string) => {
    setSkuHistory({ key: sku, events: [] })
    try {
      const r = await fetch(`${API}/catalogues/audit?sku=${encodeURIComponent(sku)}&limit=200`, { headers: authHeaders() })
      if (r.ok) { const d = await r.json(); setSkuHistory({ key: sku, events: d.events ?? [] }) }
    } catch { /* non-critical */ }
  }, [])
  const auditFiltered = useMemo(() => {
    const q = auditQuery.trim().toLowerCase()
    if (!q) return audit
    return audit.filter(e =>
      (e.sku_code ?? '').toLowerCase().includes(q) ||
      (e.display_name ?? '').toLowerCase().includes(q) ||
      (e.username ?? '').toLowerCase().includes(q) ||
      e.action.toLowerCase().includes(q) ||
      JSON.stringify(e.details ?? {}).toLowerCase().includes(q))
  }, [audit, auditQuery])

  // Delete ALL catalogues in the database (every import + its extracted items).
  // Products, tags and the audit trail are preserved server-side.
  const [deletingCatalogues, setDeletingCatalogues] = useState(false)
  async function deleteAllCatalogues() {
    const total = imports.reduce((n, i) => n + (i.item_count ?? 0), 0)
    const ok = await confirmDialog({
      title: 'Delete all catalogues',
      message: `Permanently delete all ${imports.length} catalogue import(s) and ${total} extracted item(s) from the database?\n\nProducts, tags and the audit trail are kept. This cannot be undone.`,
      confirmLabel: 'Delete all', danger: true,
    })
    if (!ok) return
    setDeletingCatalogues(true)
    try {
      const res = await fetch(`${API}/catalogues?confirm=true`, { method: 'DELETE', headers: authHeaders() })
      if (res.ok) {
        const d = await res.json()
        setQueue([]); setPendingCount(0); setSelectedImportId(null)
        await fetchAll(); fetchAudit()
        flash(`Deleted ${d.imports_deleted} catalogues · ${d.items_deleted} items`)
      } else {
        toast.error((await res.json().catch(() => ({}))).detail ?? 'Delete failed')
      }
    } finally { setDeletingCatalogues(false) }
  }

  // Remove all queued (still-pending) items waiting to be confirmed; imports + processed items stay.
  const [clearingPending, setClearingPending] = useState(false)
  async function removeQueuedItems() {
    const ok = await confirmDialog({
      title: 'Remove queued items',
      message: `Remove all ${pendingCount} queued item(s) waiting to be confirmed?\n\nThe catalogue imports and any already-processed items are kept.`,
      confirmLabel: 'Remove', danger: true,
    })
    if (!ok) return
    setClearingPending(true)
    try {
      const res = await fetch(`${API}/catalogues/queue/pending?confirm=true`, { method: 'DELETE', headers: authHeaders() })
      if (res.ok) {
        const d = await res.json()
        setQueue([]); setPendingCount(0); setSelectedImportId(null)
        await fetchAll(); fetchAudit()
        flash(`Removed ${d.items_deleted} queued items`)
      } else {
        toast.error((await res.json().catch(() => ({}))).detail ?? 'Remove failed')
      }
    } finally { setClearingPending(false) }
  }

  // Re-run the AI tagging/categorization pass on every import's pending items
  // (fixes imports uploaded before the AI pass, or refreshes after prompt changes).
  const [retagging, setRetagging] = useState(false)
  async function retagAll() {
    const pending = imports.reduce((n, imp) => n + (imp.counts?.pending ?? 0), 0)
    if (pending === 0) { flash('Nothing to tag'); return }
    const ok = await confirmDialog({
      title: 'Re-run AI tagging',
      message: `Re-run AI tagging on ${pending} pending item${pending === 1 ? '' : 's'}?`
        + ` This regenerates each item’s tags, category and subcategory (overwriting the current`
        + ` AI suggestions) and calls the Claude API.`,
      confirmLabel: 'Re-tag',
    })
    if (!ok) return
    setRetagging(true)
    try {
      let total = 0, warning: string | null = null
      for (const imp of imports) {
        if (imp.counts.pending === 0) continue
        const r = await fetch(`${API}/catalogues/${imp.id}/ai-tag`, { method: 'POST', headers: authHeaders() })
        if (r.ok) { const d = await r.json(); total += d.tagged ?? 0; if (d.warning) warning = d.warning }
      }
      await fetchAll()
      if (warning) toast.error(warning)
      else flash(total > 0 ? `AI-tagged ${total} items` : 'Nothing to tag')
    } catch { toast.error('Re-tag failed') }
    finally { setRetagging(false) }
  }

  // Batch (whole-folder) upload state
  const [batchFiles, setBatchFiles]   = useState<BatchFile[]>([])
  const [batchRunning, setBatchRunning] = useState(false)
  const [, setBatchSkipped] = useState(0)
  const [batchSupplierId, setBatchSupplierId] = useState<number | null>(null)  // override supplier for all batch files
  const batchInputRef = useRef<HTMLInputElement>(null)        // folder picker (webkitdirectory → browser upload prompt)
  const batchFilesInputRef = useRef<HTMLInputElement>(null)   // plain multi-file picker (no browser prompt)
  const batchCancelRef = useRef(false)
  // Unfinished batch recovered from a previous session (after a refresh). When set, a
  // banner offers to resume — re-picking the files reconciles against this snapshot.
  const [resumeSnap, setResumeSnap] = useState<BatchSnapshot | null>(null)

  // Persist a metadata snapshot of the live batch so it survives an accidental refresh.
  useEffect(() => {
    if (typeof window === 'undefined' || batchFiles.length === 0) return
    try {
      const snap: BatchSnapshot = { savedAt: Date.now(), files: serializeBatch(batchFiles) }
      localStorage.setItem(BATCH_SNAPSHOT_KEY, JSON.stringify(snap))
    } catch { /* quota / private mode — non-fatal */ }
  }, [batchFiles])

  // On first load, recover any unfinished batch (has files that never reached 'done').
  useEffect(() => {
    if (typeof window === 'undefined') return
    try {
      const raw = localStorage.getItem(BATCH_SNAPSHOT_KEY)
      if (!raw) return
      const snap = JSON.parse(raw) as BatchSnapshot
      const remaining = (snap.files ?? []).filter(f => f.status !== 'done')
      if (remaining.length > 0) setResumeSnap(snap)
      else localStorage.removeItem(BATCH_SNAPSHOT_KEY)
    } catch { localStorage.removeItem(BATCH_SNAPSHOT_KEY) }
  }, [])

  // Warn before leaving / refreshing while a scan or batch is in flight.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const active = uploading || batchRunning
    if (!active) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [uploading, batchRunning])

  function discardResume() {
    setResumeSnap(null)
    if (typeof window !== 'undefined') localStorage.removeItem(BATCH_SNAPSHOT_KEY)
  }

  // Set the supplier for the whole batch (overrides folder-name inference).
  function setBatchSupplier(id: number | null) {
    setBatchSupplierId(id)
    if (id != null) setBatchFiles(prev => prev.map(f => ({ ...f, supplierId: id })))
  }

  const [actions, setActions]     = useState<Record<number, ItemAction>>({})
  const [processing, setProcessing] = useState<Set<number>>(new Set())
  // Which action is in flight per item ('match' | 'assign' | 'reject' | 'edit'), so only
  // the button the reviewer clicked shows a spinner — the rest just disable.
  const [actionKind, setActionKind] = useState<Map<number, string>>(new Map())
  // Bulk-bar action in flight ('match' | 'reject' | 'reject-brand'), for spinners + disabling.
  const [bulkBusy, setBulkBusy] = useState<string | null>(null)

  function getAction(id: number): ItemAction { return actions[id] ?? defaultAction() }
  function patchAction(id: number, patch: Partial<ItemAction>) {
    setActions(prev => ({ ...prev, [id]: { ...getAction(id), ...patch } }))
  }

  // How many queue items to pull per page in the all-imports view. Selecting an import
  // always fetches that import's items in full — this is what fixes "scanned items not
  // showing": the old top-500-by-confidence global fetch silently dropped recent scans.
  const QUEUE_PAGE = 300
  const [queueTotal, setQueueTotal] = useState(0)   // items matching the current scope (server count)
  const [skippedCount, setSkippedCount] = useState(0)
  const [view, setView] = useState<'review' | 'skipped' | 'confirmed'>('review')  // queue view
  const showSkipped = view === 'skipped'                  // drives the queue fetch (skipped bucket)
  const [confirmed, setConfirmed] = useState<ConfirmedItem[]>([])
  const [confirmedCount, setConfirmedCount] = useState(0)
  const [confirmedLoading, setConfirmedLoading] = useState(false)
  const [alreadyVerified, setAlreadyVerified] = useState<{ count: number; items: AlreadyVerifiedItem[] }>({ count: 0, items: [] })
  const [showAlreadyVerified, setShowAlreadyVerified] = useState(false)   // expand the review panel
  const [skippingVerified, setSkippingVerified] = useState(false)
  const [translatingImport, setTranslatingImport] = useState<number | null>(null)
  const [dragOver, setDragOver] = useState(false)        // upload drop-zone hover
  const [manageOpen, setManageOpen] = useState(false)    // header ⋯ Manage menu
  const [showHistory, setShowHistory] = useState(false)  // bottom History area
  const [histTab, setHistTab] = useState<'daily' | 'imports' | 'scans' | 'activity'>('daily')
  const [advancedOpen, setAdvancedOpen] = useState(false)  // filter "More" popover
  const [bulkRejectReason, setBulkRejectReason] = useState('clinical_consumable')

  const fetchQueue = useCallback(async (depth?: number) => {
    // Any active filter (supplier / search / who-skipped) narrows the queue server-side to a
    // small set, so load them all; otherwise page the full queue. Filtering server-side is what
    // makes these filters cover the WHOLE queue instead of just the loaded page.
    const narrowed = selectedImportId != null || supplierFilter || queueSearch || skippedByFilter
    const limit = narrowed ? 2000 : Math.min(depth ?? QUEUE_PAGE, 2000)
    const qs = new URLSearchParams({ limit: String(limit), include_inactive: String(includeInactive), skipped: String(showSkipped) })
    if (selectedImportId != null) qs.set('import_id', String(selectedImportId))
    if (supplierFilter) qs.set('supplier_id', supplierFilter)
    if (queueSearch) qs.set('search', queueSearch)
    if (skippedByFilter) qs.set('skipped_by', skippedByFilter)
    try {
      const r = await fetch(`${API}/catalogues/queue/pending?${qs}`, { headers: authHeaders() })
      if (r.ok) {
        const d = await r.json()
        setQueue(d.items ?? [])
        setPendingCount(d.pending_count ?? 0)
        setSkippedCount(d.skipped_count ?? 0)
        setConfirmedCount(d.confirmed_count ?? 0)
        setQueueTotal(d.filtered_count ?? (d.items?.length ?? 0))
        setSupplierFacets(d.supplier_facets ?? [])
        setUserFacets(d.user_facets ?? [])
      }
    } catch { /* keep current queue on a failed poll */ }
  }, [includeInactive, selectedImportId, showSkipped, supplierFilter, queueSearch, skippedByFilter])

  const fetchAll = useCallback(async () => {
    try {
      const [supRes, impRes, bRes, slRes] = await Promise.all([
        fetch(`${API}/suppliers`, { headers: authHeaders() }),
        fetch(`${API}/catalogues`, { headers: authHeaders() }),
        fetch(`${API}/catalogues/brand-coverage`, { headers: authHeaders() }),
        fetch(`${API}/catalogues/scan-log`, { headers: authHeaders() }),
      ])
      if (supRes.ok) setSuppliers(await supRes.json())
      if (impRes.ok) setImports(await impRes.json())
      if (bRes.ok) {
        const d = await bRes.json()
        setBrandsInDb(new Set((d.brands ?? []).map((b: string) => b.toLowerCase().trim())))
      }
      if (slRes.ok) setScanLog(await slRes.json())
      await fetchQueue()
    } finally {
      setLoading(false)
    }
  }, [fetchQueue])

  // Controlled subcategory vocabulary (functional/clinical class) for the reviewer picker.
  useEffect(() => {
    fetch(`${API}/catalogues/subcategories`, { headers: authHeaders() })
      .then(r => r.ok ? r.json() : null).then(d => { if (d) setSubcatVocab(d.subcategories ?? []) }).catch(() => {})
  }, [])

  // Refetch the queue when its scope changes (import selected / include-inactive toggled).
  const didInitQueue = useRef(false)
  useEffect(() => {
    if (!didInitQueue.current) { didInitQueue.current = true; return }   // mount handled by fetchAll
    fetchQueue()
  }, [fetchQueue])

  // Realtime-ish multi-user sync: other reviewers' confirmations remove items and new scans
  // appear without a manual refresh. Light poll of imports + the current queue scope.
  const queueDepthRef = useRef(0)
  useEffect(() => { queueDepthRef.current = queue.length }, [queue.length])
  useEffect(() => {
    const t = setInterval(() => {
      if (document.hidden || loading) return
      fetchQueue(Math.max(queueDepthRef.current, QUEUE_PAGE))
      fetch(`${API}/catalogues`, { headers: authHeaders() })
        .then(r => r.ok ? r.json() : null).then(d => { if (d) setImports(d) }).catch(() => {})
    }, 12000)
    return () => clearInterval(t)
  }, [fetchQueue, loading])

  // Stage-1 supplier confirmation: the selected import + whether its supplier still needs confirming
  const selectedImport = useMemo(
    () => (selectedImportId == null ? null : imports.find(i => i.id === selectedImportId) ?? null),
    [imports, selectedImportId])
  const needsSupplierConfirm = !!selectedImport && selectedImport.supplier_status === 'needs_review'
  const [supplierChoice, setSupplierChoice] = useState<number | ''>('')
  const [confirmingSupplier, setConfirmingSupplier] = useState(false)
  useEffect(() => {
    setSupplierChoice(selectedImport?.supplier_id ?? '')
  }, [selectedImportId, selectedImport?.supplier_id])

  const confirmSupplier = useCallback(async (importId: number, supplierId: number) => {
    setConfirmingSupplier(true)
    try {
      const res = await fetch(`${API}/catalogues/${importId}/supplier`, {
        method: 'PATCH',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ supplier_id: supplierId, reviewed_by: getUser()?.username }),
      })
      if (res.ok) { await fetchAll(); fetchAudit() }   // reload -> status becomes 'confirmed' -> SKU stage unlocks
    } finally {
      setConfirmingSupplier(false)
    }
  }, [fetchAll])

  // Derived: items for the currently-selected import (or all if none selected).
  // Stage 1 gate: hide SKUs until the supplier is confirmed.
  const scopedQueue = useMemo(() => {
    if (selectedImportId == null) return queue
    if (needsSupplierConfirm) return []
    return queue.filter(q => q.import_id === selectedImportId)
  }, [queue, selectedImportId, needsSupplierConfirm])

  // Tier-classify every item once (memoized)
  const itemsWithTier = useMemo(() => {
    return scopedQueue.map(q => ({ ...q, tier: classifyTier(q, brandsInDb) }))
  }, [scopedQueue, brandsInDb])

  const tierCounts = useMemo(() => {
    const c: Record<TierId, number> = { t1a: 0, t1b: 0, t2a: 0, t2b: 0, t3: 0, t4: 0 }
    itemsWithTier.forEach(i => { c[i.tier]++ })
    return c
  }, [itemsWithTier])

  const allBrandsInQueue = useMemo(() => {
    const s = new Set<string>()
    itemsWithTier.forEach(i => { if (i.brand) s.add(i.brand) })
    return Array.from(s).sort()
  }, [itemsWithTier])

  // The currently-displayed list, after filter chips + brand filter
  const visibleItems = useMemo(() => {
    const q = itemSearch.trim().toLowerCase()
    return itemsWithTier.filter(i => {
      if (filter === 'match'   && (!i.suggested_matches || i.suggested_matches.length === 0)) return false
      if (filter === 'nomatch' && (i.suggested_matches?.length ?? 0) > 0) return false
      if (filter === 'active'  && i.suggested_matches?.[0]?.status !== 'ACTIVE') return false
      if (filter === 't1a' && i.tier !== 't1a') return false
      if (filter === 't1b' && i.tier !== 't1b') return false
      if (filter === 't1' && i.tier !== 't1a' && i.tier !== 't1b') return false
      if (filter === 't2a' && i.tier !== 't2a') return false
      if (filter === 't2b' && i.tier !== 't2b') return false
      if (filter === 't2' && i.tier !== 't2a' && i.tier !== 't2b') return false
      if (filter === 't3' && i.tier !== 't3') return false
      if (filter === 't4' && i.tier !== 't4') return false
      if (brandFilter && i.brand !== brandFilter) return false
      if (supplierFilter && String(i.supplier_id ?? '') !== supplierFilter) return false
      if (q) {
        const hay = [i.raw_description, i.supplier_sku, i.barcode, i.brand, i.ai_category,
                     i.ai_subcategory, i.suggested_matches?.[0]?.sku_code, i.suggested_matches?.[0]?.name,
                     ...(i.ai_tags ?? [])].filter(Boolean).join(' ').toLowerCase()
        if (!hay.includes(q)) return false
      }
      return true
    }).sort((a, b) => {
      // High-confidence matches first, then medium, then no-match
      const ca = a.suggested_matches?.[0]?.confidence ?? 0
      const cb = b.suggested_matches?.[0]?.confidence ?? 0
      return cb - ca
    })
  }, [itemsWithTier, filter, brandFilter, supplierFilter, itemSearch])

  // Any active review-queue filter/search — keeps the filter bar + a "no matches" notice visible
  // even when the (now server-side) search returns zero items, so it's never a dead end.
  const anyQueueFilter = !!itemSearch || !!supplierFilter || !!brandFilter || filter !== 'all'

  // Render pagination — the per-item cards are heavy, so mounting hundreds at once is what
  // made filtering/search feel slow. Filters still apply to the FULL loaded set; we just
  // mount the first N cards and grow on demand.
  const RENDER_STEP = 30
  const [renderCount, setRenderCount] = useState(RENDER_STEP)
  useEffect(() => { setRenderCount(RENDER_STEP) },
    [filter, brandFilter, supplierFilter, itemSearch, selectedImportId])
  const shownItems = useMemo(() => visibleItems.slice(0, renderCount), [visibleItems, renderCount])

  // Bulk approve all T1 (95%+) items globally — scoped by current filter set
  const t1ItemsInScope = itemsWithTier.filter(i => i.tier === 't1a' || i.tier === 't1b')

  const batchStats = useMemo(() => {
    let done = 0, error = 0, uploading = 0, queued = 0, items = 0, matched = 0
    const sups = new Set<string>()
    for (const b of batchFiles) {
      if (b.status === 'done')        { done++; items += b.itemCount ?? 0 }
      else if (b.status === 'error')   error++
      else if (b.status === 'uploading') uploading++
      else                             queued++
      if (b.supplierId != null) matched++
      if (b.supplierFolder) sups.add(b.supplierFolder)
    }
    return { total: batchFiles.length, done, error, uploading, queued, items, matched, suppliers: sups.size }
  }, [batchFiles])

  // Group the batch by supplier folder so each supplier can be retried independently.
  const batchGroups = useMemo(() => {
    const map = new Map<string, BatchFile[]>()
    for (const b of batchFiles) {
      const k = b.supplierFolder || '—'
      if (!map.has(k)) map.set(k, [])
      map.get(k)!.push(b)
    }
    return Array.from(map.entries())
      .map(([folder, files]) => ({
        folder,
        files,
        total: files.length,
        done:  files.filter(f => f.status === 'done').length,
        error: files.filter(f => f.status === 'error').length,
        items: files.reduce((n, f) => n + (f.status === 'done' ? (f.itemCount ?? 0) : 0), 0),
        matched: files.some(f => f.supplierId != null),
      }))
      .sort((a, b) => a.folder.localeCompare(b.folder))
  }, [batchFiles])

  useEffect(() => { fetchAll() }, [fetchAll])
  useEffect(() => { refreshNextSku() }, [refreshNextSku])
  useEffect(() => { fetchAudit() }, [fetchAudit])
  // Load the daily report on mount, and refresh it whenever the Daily tab is reopened.
  useEffect(() => { if (histTab === 'daily') fetchDaily() }, [histTab, fetchDaily])

  // ── Near-realtime updates ───────────────────────────────────────────────
  // While a batch runs, poll so the queue, scan log and activity fill in live
  // (each import lands synchronously, so every refresh surfaces newly-scanned
  // files without the user having to reload).
  useEffect(() => {
    if (!batchRunning) return
    setShowScanLog(true)               // auto-open the scan details so they update in view
    const id = setInterval(() => { fetchAll(); fetchAudit() }, 6000)
    return () => clearInterval(id)
  }, [batchRunning, fetchAll, fetchAudit])

  // Catch anything that landed while the batch finished / the tab was away.
  useEffect(() => {
    const refresh = () => { if (!document.hidden) { fetchAll(); fetchAudit() } }
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [fetchAll, fetchAudit])

  // ── Batch upload ──────────────────────────────────────────────────────────
  // Enable folder selection on the hidden input (non-standard attrs, set via ref).
  useEffect(() => {
    const el = batchInputRef.current
    if (el) { el.setAttribute('webkitdirectory', ''); el.setAttribute('directory', '') }
  }, [])

  function handleBatchSelect(e: React.ChangeEvent<HTMLInputElement>) {
    ingestFiles(Array.from(e.target.files ?? []))
  }
  function handleDrop(e: React.DragEvent) {
    e.preventDefault(); setDragOver(false)
    if (!batchRunning) ingestFiles(Array.from(e.dataTransfer.files ?? []))
  }

  function ingestFiles(files: File[]) {
    let skipped = 0
    const picked: BatchFile[] = []
    for (const f of files) {
      const ext = (f.name.split('.').pop() ?? '').toLowerCase()
      if (f.name.startsWith('.') || !BATCH_EXT.has(ext)) { skipped++; continue }
      const relPath = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name
      const folder = inferSupplierFolder(relPath)
      picked.push({
        key: `${relPath}:${f.size}`,
        file: f, name: f.name,
        supplierFolder: folder,
        supplierId: batchSupplierId ?? matchSupplierId(folder, suppliers),
        status: 'queued', itemCount: null, importId: null, error: null,
        fmt: null, detectedSupplier: null, detectedBrands: null, supplierStatus: null,
        sizeMB: f.size / 1e6, startedAt: null,
      })
    }
    picked.sort((a, b) =>
      a.supplierFolder.localeCompare(b.supplierFolder) || a.name.localeCompare(b.name))

    // Resuming an interrupted batch: carry over files that already extracted last session so
    // we don't re-scan them (which would create duplicate imports). Match by name+size, and
    // also treat any file already present in the server's import history as done.
    if (resumeSnap) {
      const doneById = new Map<string, SerializedBatchFile>()
      for (const sf of resumeSnap.files) {
        if (sf.status === 'done') doneById.set(fileMatchId(sf), sf)
      }
      const importedNames = new Set(imports.map(i => i.filename))
      for (const p of picked) {
        const snap = doneById.get(fileMatchId(p))
        if (snap) {
          p.status = 'done'; p.error = null
          p.itemCount = snap.itemCount; p.importId = snap.importId; p.fmt = snap.fmt
          p.detectedSupplier = snap.detectedSupplier; p.detectedBrands = snap.detectedBrands
          p.supplierStatus = snap.supplierStatus
        } else if (importedNames.has(p.name)) {
          p.status = 'done'   // landed server-side even if the snapshot missed it (interrupted mid-extract)
        }
      }
      const remaining = picked.filter(p => p.status !== 'done').length
      setResumeSnap(null)
      toast.info(remaining > 0 ? `Resumed — ${remaining} file(s) left to scan` : 'All files already scanned')
    }

    setBatchFiles(picked)
    setBatchSkipped(skipped)
  }

  async function uploadOne(bf: BatchFile) {
    const startedAt = Date.now()
    setBatchFiles(prev => prev.map(x => x.key === bf.key ? { ...x, status: 'uploading', error: null, startedAt } : x))
    // v1 POST /catalogues/import (synchronous AI extraction) was removed. Files
    // now enter the evidence-first queued pipeline, which REQUIRES a supplier
    // identity to resolve the supplier-source contract.
    if (bf.supplierId == null) {
      setBatchFiles(prev => prev.map(x => x.key === bf.key
        ? { ...x, status: 'error', error: 'Supplier required — assign a supplier before ingesting' }
        : x))
      return
    }
    try {
      const fd = new FormData()
      fd.append('file', bf.file)
      fd.append('supplier_id', String(bf.supplierId))
      const res = await fetch(`${API}/catalogues/ingestions`, { method: 'POST', body: fd, headers: authHeaders() })
      const data = await res.json().catch(() => ({}))
      setBatchFiles(prev => prev.map(x => x.key === bf.key
        ? (res.ok
            ? { ...x, status: 'done', itemCount: null, importId: null,
                fmt: data.document_type ?? null,
                detectedSupplier: null,
                detectedBrands: null,
                supplierStatus: data.contract_id ? `queued (${data.contract_id})` : 'queued' }
            : { ...x, status: 'error', error: (typeof data.detail === 'string' ? data.detail : data.detail?.message) ?? `HTTP ${res.status}` })
        : x))
    } catch {
      setBatchFiles(prev => prev.map(x => x.key === bf.key ? { ...x, status: 'error', error: 'Network error' } : x))
    }
  }

  // Core runner — uploads the given files through a small concurrency pool.
  // Used by the full run and by every targeted retry (file / supplier / all-failed).
  async function processFiles(todo: BatchFile[]) {
    if (batchRunning || todo.length === 0) return
    setBatchRunning(true)
    batchCancelRef.current = false
    // Reset the targeted files to 'queued' first (so a retried 'error'/'done' row
    // visibly goes back to queued before re-extracting).
    const keys = new Set(todo.map(t => t.key))
    setBatchFiles(prev => prev.map(x => keys.has(x.key) ? { ...x, status: 'queued', error: null } : x))
    const CONCURRENCY = 3
    let idx = 0
    const worker = async () => {
      while (!batchCancelRef.current) {
        const i = idx++
        if (i >= todo.length) return
        await uploadOne(todo[i])
      }
    }
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, todo.length) }, worker))
    setBatchRunning(false)
    fetchAll()   // refresh queue / scan-log with everything that landed
  }

  // Full run: everything not yet done (queued + any prior failures).
  const runBatch = () => processFiles(batchFiles.filter(b => b.status === 'queued' || b.status === 'error'))
  // Retry scopes.
  const retryAllFailed   = () => processFiles(batchFiles.filter(b => b.status === 'error'))
  const retrySupplier    = (folder: string) =>
    processFiles(batchFiles.filter(b => b.supplierFolder === folder && b.status === 'error'))
  const redoFile = (key: string) => {
    const f = batchFiles.find(b => b.key === key)
    if (f) processFiles([f])
  }

  function cancelBatch() { batchCancelRef.current = true }

  function clearBatch() {
    if (batchRunning) return
    setBatchFiles([]); setBatchSkipped(0)
    if (typeof window !== 'undefined') localStorage.removeItem(BATCH_SNAPSHOT_KEY)
    if (batchInputRef.current) batchInputRef.current.value = ''
    if (batchFilesInputRef.current) batchFilesInputRef.current.value = ''
  }

  function removeFromQueue(id: number) {
    setQueue(prev => prev.filter(i => i.id !== id))
    setPendingCount(prev => Math.max(0, prev - 1))
  }

  function setBusy(id: number, busy: boolean, kind?: string) {
    setProcessing(prev => { const s = new Set(prev); busy ? s.add(id) : s.delete(id); return s })
    setActionKind(prev => {
      const m = new Map(prev)
      if (busy && kind) m.set(id, kind); else m.delete(id)
      return m
    })
  }
  const busyKind = (id: number): string | undefined => actionKind.get(id)

  function reviewer(): string {
    return getUser()?.display_name ?? 'Unknown'
  }

  // Species via Claude + web search — researches the brand/product for accuracy. On demand
  // (slower than the bulk tagging pass), so the reviewer triggers it per item when unsure.
  const [speciesBusy, setSpeciesBusy] = useState<Set<number>>(new Set())
  async function detectSpecies(item: QueueItem) {
    setSpeciesBusy(prev => new Set(prev).add(item.id))
    try {
      const r = await fetch(`${API}/catalogues/items/${item.id}/detect-species`, { method: 'POST', headers: authHeaders() })
      if (r.ok) {
        const d = await r.json()
        if (d.species) {
          setQueue(prev => prev.map(q => q.id === item.id ? { ...q, species: d.species } : q))
          toast.success(`Species: ${d.species}`)
        } else { toast.info('Could not determine species from the web') }
      } else { toast.error('Species detection failed') }
    } catch { toast.error('Species detection failed') }
    finally { setSpeciesBusy(prev => { const n = new Set(prev); n.delete(item.id); return n }) }
  }

  // Debounced inventory search for the Find-&-Match picker — lets the reviewer replace the
  // suggested match (or match a no-match scan) with any inventory item, by name/brand/SKU.
  function runSkuSearch(itemId: number, query: string) {
    if (skuTimers.current[itemId]) clearTimeout(skuTimers.current[itemId])
    const q = query.trim()
    if (q.length < 2) { setSkuResults(p => ({ ...p, [itemId]: [] })); return }
    skuTimers.current[itemId] = setTimeout(async () => {
      setSkuSearching(p => ({ ...p, [itemId]: true }))
      try {
        const res = await fetch(`${API}/products?search=${encodeURIComponent(q)}&include_inactive=true&limit=15`,
          { headers: authHeaders() })
        if (res.ok) {
          const d = await res.json()
          setSkuResults(p => ({ ...p, [itemId]: (d.items ?? []).slice(0, 15) }))
        }
      } catch { /* leave prior results */ }
      finally { setSkuSearching(p => ({ ...p, [itemId]: false })) }
    }, 250)
  }

  // Find & Match: select a SKU as the pending match (does NOT confirm). The reviewer then
  // reviews the side-by-side and clicks "Confirm match".
  function pickMatch(item: QueueItem, r: SkuResult) {
    patchAction(item.id, { mode: 'idle', pickedMatch: asPickedMatch(r) })
  }

  async function doMatch(item: QueueItem, skuCode: string, label?: string) {
    const trimmed = skuCode.trim()
    if (!trimmed) return
    const a = getAction(item.id)
    const rename = a.matchName.trim() && a.matchName.trim() !== (label ?? '') ? a.matchName.trim() : ''
    const ok = await confirmDialog({
      title: 'Confirm match',
      message: `Match scanned item “${item.raw_description ?? 'this line'}” to inventory SKU ${trimmed}`
        + `${label ? ` — ${label}` : ''}?`
        + ` This writes the scan’s cost${item.cost_price != null ? ` (HK$${item.cost_price.toFixed(0)})` : ''},`
        + ` supplier SKU${item.supplier_sku ? ` “${item.supplier_sku}”` : ''}, brand and tags to that item,`
        + `${rename ? ` renames its title to “${rename}”,` : ''}`
        + ` and removes the line from the queue.`,
      confirmLabel: 'Match',
    })
    if (!ok) return
    setBusy(item.id, true, 'match')
    try {
      const res = await fetch(`${API}/catalogues/items/${item.id}/match`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        // Send the reviewer's confirmed fields so matching updates the inventory product
        // (brand falls back to the item's edited brand server-side).
        body: JSON.stringify({
          sku_code: trimmed, reviewed_by: reviewer(),
          // Reviewer edits win; else the matched product's REAL (shopify) tags; else AI.
          tags: a.tags ?? (item.suggested_matches?.[0]?.tags?.length ? item.suggested_matches[0].tags : null) ?? item.ai_tags ?? [],
          category: effectiveCategory(a, item),
          subcategory: effectiveSubcategory(a, item) || undefined,
          brand: a.brand || item.brand || undefined,
          name: rename || undefined,          // optional SKU-title rename
        }),
      })
      if (res.ok) { removeFromQueue(item.id); fetchAudit() }
      else { const e = await res.json(); toast.error(e.detail ?? 'Match failed') }
    } finally { setBusy(item.id, false) }
  }

  // Inline supplier re-assignment from the scan card (#3): the cost + supplier SKU land on
  // this supplier when the line is confirmed, so getting it right matters.
  async function changeItemSupplier(item: QueueItem, supplierId: number | null) {
    if (supplierId == null || supplierId === item.supplier_id) return
    const supName = suppliers.find(s => s.id === supplierId)?.name ?? `#${supplierId}`
    const ok = await confirmDialog({
      title: 'Change supplier',
      message: `Assign “${item.raw_description ?? 'this line'}” to ${supName}? On confirm, the cost and supplier SKU are written against this supplier.`,
      confirmLabel: 'Change supplier',
    })
    if (!ok) return
    setBusy(item.id, true, 'edit')
    try {
      const res = await fetch(`${API}/catalogues/items/${item.id}?include_inactive=${includeInactive}`, {
        method: 'PATCH',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ supplier_id: supplierId }),
      })
      if (res.ok) {
        const updated = await res.json()
        setQueue(prev => prev.map(q => q.id === item.id ? { ...q, ...updated } : q))
        toast.success(`Supplier → ${supName}`)
      } else { const e = await res.json().catch(() => ({})); toast.error(e.detail ?? 'Supplier change failed') }
    } finally { setBusy(item.id, false) }
  }

  async function doAssignNew(item: QueueItem) {
    const a = getAction(item.id)
    const ok = await confirmDialog({
      title: 'Create new SKU',
      message: `Create a new ${effectiveCategory(a, item)} SKU for “${a.name || item.raw_description || 'this item'}”`
        + `${(a.brand || item.brand) ? ` (${a.brand || item.brand})` : ''}?`
        + ` This adds a brand-new inventory item and removes the line from the queue.`,
      confirmLabel: 'Create SKU',
    })
    if (!ok) return
    setBusy(item.id, true, 'assign')
    try {
      const res = await fetch(`${API}/catalogues/items/${item.id}/assign-new`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          category: effectiveCategory(a, item),
          name: a.name || undefined,
          brand: a.brand || item.brand || undefined,   // scanned/edited brand when not retyped
          uom: item.uom || undefined,
          reviewed_by: reviewer(),
          tags: effectiveTags(a, item),
          subcategory: effectiveSubcategory(a, item) || undefined,
        }),
      })
      if (res.ok) {
        const d = await res.json().catch(() => ({}))
        removeFromQueue(item.id)
        if (d.sku_code) flash(`Created SKU ${d.sku_code} · ${d.category ?? effectiveCategory(a, item)}`)
        refreshNextSku()   // counter advanced — refresh the preview for the next item
        fetchAudit()
      }
      else { const e = await res.json(); toast.error(e.detail ?? 'Failed to create SKU') }
    } finally { setBusy(item.id, false) }
  }

  // ── Bulk actions ──────────────────────────────────────────────────────────
  function toggleSelect(id: number) {
    setSelectedIds(prev => { const s = new Set(prev); s.has(id) ? s.delete(id) : s.add(id); return s })
  }
  function selectAll(ids: number[]) { setSelectedIds(new Set(ids)) }
  function clearSelection() { setSelectedIds(new Set()) }

  async function doBulkApproveTier1() {
    // Full-DB: the server matches EVERY pending item with a ≥95% match across the whole queue,
    // not just the items the page has loaded.
    const ok = await confirmDialog({
      title: 'Approve high-confidence matches',
      message: `Auto-approve and match every pending item with a ≥95% match to its suggested SKU — across the entire queue${selectedImportId != null ? ' for this import' : ''}, not just the items loaded here. Each updates the linked inventory item.`,
      confirmLabel: 'Approve all high-confidence',
    })
    if (!ok) return
    setBulkBusy('match')
    try {
      const res = await fetch(`${API}/catalogues/items/match-confident`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ import_id: selectedImportId ?? undefined, reviewed_by: reviewer() }),
      })
      if (res.ok) {
        const data = await res.json()
        await fetchQueue(); fetchAudit(); fetchAlreadyVerified(); clearSelection()
        toast.success(data.matched > 0
          ? `Reconciled ${data.matched} high-confidence match${data.matched === 1 ? '' : 'es'} across the queue`
          : 'No high-confidence matches to reconcile')
      } else { const e = await res.json().catch(() => ({})); toast.error(e.detail ?? 'Bulk approve failed') }
    } catch { toast.error('Bulk approve failed') }
    finally { setBulkBusy(null) }
  }

  async function doBulkMatchSelected() {
    const items = visibleItems.filter(i => selectedIds.has(i.id) && i.suggested_matches?.[0])
    if (items.length === 0) return
    const matches = items.map(i => ({ item_id: i.id, sku_code: i.suggested_matches[0].sku_code, tags: effectiveTags(getAction(i.id), i) }))
    const ok = await confirmDialog({
      title: 'Match selected items',
      message: `Match ${items.length} selected item${items.length === 1 ? '' : 's'} to their top suggested SKU? Each updates the linked inventory item and is removed from the queue.`,
      confirmLabel: `Match ${items.length}`,
    })
    if (!ok) return
    setBulkBusy('match')
    try {
      const res = await fetch(`${API}/catalogues/items/bulk-match`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ matches, reviewed_by: reviewer() }),
      })
      if (res.ok) {
        const data = await res.json()
        setQueue(prev => prev.filter(q => !matches.find(m => m.item_id === q.id)))
        setPendingCount(prev => Math.max(0, prev - data.matched))
        clearSelection(); fetchAudit()
        toast.success(`Matched ${data.matched} item${data.matched === 1 ? '' : 's'}`)
      } else { toast.error('Bulk match failed') }
    } catch { toast.error('Bulk match failed') }
    finally { setBulkBusy(null) }
  }

  async function doBulkRejectSelected(reason: string) {
    const ids = Array.from(selectedIds)
    if (ids.length === 0) return
    const ok = await confirmDialog({
      title: 'Reject selected items',
      message: `Reject ${ids.length} selected item${ids.length === 1 ? '' : 's'} as “${reason.replace(/_/g, ' ')}”? They will be removed from the queue.`,
      confirmLabel: `Reject ${ids.length}`,
      danger: true,
    })
    if (!ok) return
    setBulkBusy('reject')
    try {
      const res = await fetch(`${API}/catalogues/items/bulk-reject`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ item_ids: ids, reason, reviewed_by: reviewer() }),
      })
      if (res.ok) {
        const data = await res.json()
        setQueue(prev => prev.filter(q => !ids.includes(q.id)))
        setPendingCount(prev => Math.max(0, prev - data.rejected))
        clearSelection(); fetchAudit()
        toast.success(`Rejected ${data.rejected} item${data.rejected === 1 ? '' : 's'}`)
      } else { toast.error('Bulk reject failed') }
    } catch { toast.error('Bulk reject failed') }
    finally { setBulkBusy(null) }
  }

  async function doRejectBrand(brand: string) {
    // Full-DB: the server rejects every unmatched item of this brand across the whole queue,
    // not just the ones the page has loaded.
    const ok = await confirmDialog({
      title: 'Reject brand',
      message: `Reject every unmatched “${brand}” item (brand not carried) across the entire queue${selectedImportId != null ? ' for this import' : ''}, not just the ones loaded here? They will be removed from the queue.`,
      confirmLabel: `Reject all “${brand}”`,
      danger: true,
    })
    if (!ok) return
    setBulkBusy('reject-brand')
    try {
      const res = await fetch(`${API}/catalogues/items/reject-brand`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ brand, import_id: selectedImportId ?? undefined, reason: `brand_not_carried:${brand}`, reviewed_by: reviewer() }),
      })
      if (res.ok) {
        const data = await res.json()
        await fetchQueue(); fetchAudit()
        toast.success(data.rejected > 0
          ? `Rejected ${data.rejected} unmatched “${brand}” item${data.rejected === 1 ? '' : 's'} across the queue`
          : `No unmatched “${brand}” items to reject`)
      } else { toast.error('Bulk reject failed') }
    } catch { toast.error('Bulk reject failed') }
    finally { setBulkBusy(null) }
  }

  async function doReject(item: QueueItem) {
    const a = getAction(item.id)
    const ok = await confirmDialog({
      title: 'Reject item',
      message: `Reject “${item.raw_description ?? 'this line'}” as ${a.rejectReason.replace(/_/g, ' ')}?`
        + ` It will be removed from the queue.`,
      confirmLabel: 'Reject',
      danger: true,
    })
    if (!ok) return
    setBusy(item.id, true, 'reject')
    try {
      const res = await fetch(`${API}/catalogues/items/${item.id}/reject`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ reason: a.rejectReason, reviewed_by: reviewer() }),
      })
      if (res.ok) { removeFromQueue(item.id); fetchAudit() }
    } finally { setBusy(item.id, false) }
  }

  // Skip → set aside for later (no confirm; reversible). Removes it from the current view.
  async function doSkip(item: QueueItem) {
    setBusy(item.id, true, 'skip')
    try {
      const res = await fetch(`${API}/catalogues/items/${item.id}/skip`, { method: 'POST', headers: authHeaders() })
      if (res.ok) { setQueue(prev => prev.filter(q => q.id !== item.id)); setPendingCount(p => Math.max(0, p - 1)); setSkippedCount(s => s + 1); toast.success('Skipped — find it in the Skipped bucket') }
      else toast.error('Could not skip')
    } finally { setBusy(item.id, false) }
  }
  async function doUnskip(item: QueueItem) {
    setBusy(item.id, true, 'skip')
    try {
      const res = await fetch(`${API}/catalogues/items/${item.id}/unskip`, { method: 'POST', headers: authHeaders() })
      if (res.ok) { setQueue(prev => prev.filter(q => q.id !== item.id)); setSkippedCount(s => Math.max(0, s - 1)); setPendingCount(p => p + 1); toast.success('Returned to the review queue') }
      else toast.error('Could not un-skip')
    } finally { setBusy(item.id, false) }
  }

  // ── Confirmed list — items already matched / assigned a new SKU ──────────────
  const fetchConfirmed = useCallback(async () => {
    setConfirmedLoading(true)
    const qs = new URLSearchParams({ limit: '2000' })
    if (selectedImportId != null) qs.set('import_id', String(selectedImportId))
    if (confSupplier) qs.set('supplier_id', confSupplier)
    if (confUser) qs.set('reviewed_by', confUser)
    if (confSearch) qs.set('search', confSearch)
    try {
      const r = await fetch(`${API}/catalogues/confirmed?${qs}`, { headers: authHeaders() })
      if (r.ok) {
        const d = await r.json()
        setConfirmed(d.items ?? [])             // badge count comes from fetchQueue (global)
        setConfSupplierFacets(d.supplier_facets ?? [])
        setConfUserFacets(d.user_facets ?? [])
      }
    } catch { /* keep current list on a failed poll */ }
    finally { setConfirmedLoading(false) }
  }, [selectedImportId, confSupplier, confUser, confSearch])

  // Load the Confirmed list when the user opens that view (and when the scope changes there).
  useEffect(() => { if (view === 'confirmed') fetchConfirmed() }, [view, fetchConfirmed])

  // Debounce the two search boxes into their server-side query params.
  useEffect(() => { const t = setTimeout(() => setQueueSearch(itemSearch.trim()), 300); return () => clearTimeout(t) }, [itemSearch])
  useEffect(() => { const t = setTimeout(() => setConfSearch(confSearchInput.trim()), 300); return () => clearTimeout(t) }, [confSearchInput])

  // Undo a confirmation: the item returns to the review queue and its SKU's HITL-verified
  // status is dropped. The created/updated product is preserved.
  async function doUnconfirm(it: ConfirmedItem) {
    setBusy(it.id, true, 'unconfirm')
    try {
      const res = await fetch(`${API}/catalogues/items/${it.id}/unconfirm`, { method: 'POST', headers: authHeaders() })
      if (res.ok) {
        setConfirmed(prev => prev.filter(c => c.id !== it.id))
        setConfirmedCount(c => Math.max(0, c - 1))
        setPendingCount(p => p + 1)
        toast.success('Unconfirmed — back in the review queue')
      } else {
        const e = await res.json().catch(() => null)
        toast.error(e?.detail || 'Could not unconfirm')
      }
    } finally { setBusy(it.id, false) }
  }

  // ── Already-verified detector ───────────────────────────────────────────────
  // Flags pending items whose top match is a SKU that's ALREADY HITL-verified (a re-upload
  // of products you've onboarded). Skipping them marks them matched to that SKU — they leave
  // the queue and land in Confirmed (NOT the Skipped bucket), so you never re-review them.
  const fetchAlreadyVerified = useCallback(async () => {
    const qs = new URLSearchParams({ limit: '1000' })
    if (selectedImportId != null) qs.set('import_id', String(selectedImportId))
    try {
      const r = await fetch(`${API}/catalogues/already-verified?${qs}`, { headers: authHeaders() })
      if (r.ok) { const d = await r.json(); setAlreadyVerified({ count: d.count ?? 0, items: d.items ?? [] }) }
    } catch { /* non-critical */ }
  }, [selectedImportId])

  async function doSkipAlreadyVerified() {
    const ids = alreadyVerified.items.map(i => i.id)
    if (!ids.length) return
    setSkippingVerified(true)
    try {
      const r = await fetch(`${API}/catalogues/skip-already-verified`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ item_ids: ids, min_confidence: 0.9 }),
      })
      if (r.ok) {
        const d = await r.json()
        const n = d.skipped_verified ?? 0
        setQueue(prev => prev.filter(q => !ids.includes(q.id)))
        setPendingCount(p => Math.max(0, p - n))
        setConfirmedCount(c => c + n)
        setAlreadyVerified({ count: 0, items: [] })
        setShowAlreadyVerified(false)
        toast.success(`Skipped ${n} already-verified SKU${n === 1 ? '' : 's'} — marked verified (not in the Skipped bucket)`)
        fetchQueue()
      } else toast.error('Could not skip already-verified items')
    } catch { toast.error('Could not skip already-verified items') }
    finally { setSkippingVerified(false) }
  }

  // Detect already-verified duplicates on load + scope change (not on the realtime poll).
  useEffect(() => { fetchAlreadyVerified() }, [fetchAlreadyVerified])

  // Translate an already-scanned import's pending items to English (skips already-English ones).
  async function doTranslateImport(id: number) {
    setTranslatingImport(id)
    try {
      const r = await fetch(`${API}/catalogues/${id}/translate`, { method: 'POST', headers: authHeaders() })
      if (r.ok) {
        const d = await r.json()
        if (d.translated > 0) { toast.success(`Translated ${d.translated} item${d.translated === 1 ? '' : 's'} to English`); fetchQueue() }
        else toast.success('Nothing to translate — all pending items are already English')
      } else toast.error('Translation failed')
    } catch { toast.error('Translation failed') }
    finally { setTranslatingImport(null) }
  }

  function patchEdit(id: number, patch: Partial<EditDraft>) {
    const a = getAction(id)
    const base = a.edit ?? seedEdit(queue.find(q => q.id === id)!)
    patchAction(id, { edit: { ...base, ...patch } })
  }

  // Correct mis-extracted fields, then PATCH. The response carries the item with
  // freshly recomputed suggested_matches — we splice it back into the queue so the
  // diff / tier / match suggestions re-render in place (the review "loop").
  async function doEdit(item: QueueItem) {
    const ed = getAction(item.id).edit
    if (!ed) return
    const num = (s: string, int = false) => {
      const t = s.trim()
      if (t === '') return null
      const n = int ? parseInt(t, 10) : parseFloat(t)
      return Number.isFinite(n) ? n : null
    }
    const ok = await confirmDialog({
      title: 'Save changes',
      message: `Save your edits to “${ed.raw_description || item.raw_description || 'this line'}”?`
        + ` The scanned fields are updated and match suggestions re-rank — the item stays in the queue for review.`,
      confirmLabel: 'Save',
    })
    if (!ok) return
    setBusy(item.id, true, 'edit')
    try {
      const res = await fetch(`${API}/catalogues/items/${item.id}?include_inactive=${includeInactive}`, {
        method: 'PATCH',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          raw_description: ed.raw_description,
          brand:          ed.brand,
          variant:        ed.variant,
          supplier_sku:   ed.supplier_sku,
          barcode:        ed.barcode,
          uom:            ed.uom,
          min_sellable_qty: num(ed.min_sellable_qty, true),
          bulk_buy_tiers: ed.bulk_buy_tiers,
          cost_price:     num(ed.cost_price),
          units_per_pack: num(ed.units_per_pack, true),
          species:        ed.species,
          weight_grams:   num(ed.weight_value) != null ? unitToG(num(ed.weight_value)!, ed.weight_unit) : null,
          weight_unit:    ed.weight_unit || 'kg',
          rrp:            num(ed.rrp),
          min_purchase_qty: num(ed.min_purchase_qty, true),
          pack_size:      ed.pack_size,
          max_bulk_buy_cost: num(ed.max_bulk_buy_cost),
          max_bulk_buy_min_qty: num(ed.max_bulk_buy_min_qty, true),
          supplier_id:    ed.supplier_id ? parseInt(ed.supplier_id, 10) : undefined,
        }),
      })
      if (res.ok) {
        const updated = await res.json()
        // Keep import_filename (not returned by PATCH) by merging over the existing row.
        setQueue(prev => prev.map(q => q.id === item.id ? { ...q, ...updated } : q))
        patchAction(item.id, { mode: 'idle', edit: null })
      } else {
        const e = await res.json().catch(() => ({}))
        toast.error(e.detail ?? 'Edit failed')
      }
    } finally { setBusy(item.id, false) }
  }

  return (
    <>
      <style>{`
        .cat-item:hover { background: #FAFAFA !important; }
        input:focus, select:focus { border-color: #6366F1 !important; outline: none; box-shadow: 0 0 0 3px rgba(99,102,241,0.1); }
        @keyframes ims-pulse { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: .35; transform: scale(.7); } }
        .ims-live-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#22C55E; animation: ims-pulse 1.1s ease-in-out infinite; }
        @keyframes ims-shimmer { 0% { background-position: -400px 0; } 100% { background-position: 400px 0; } }
        .ims-skeleton { background: linear-gradient(90deg, #F1F5F9 25%, #E9EEF5 37%, #F1F5F9 63%); background-size: 800px 100%; animation: ims-shimmer 1.3s linear infinite; }
        @keyframes ims-fade-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
        .ims-fade-in { animation: ims-fade-in .28s ease both; }
        .ims-card { border: 1px solid #E8EDF3; border-radius: 12px; box-shadow: 0 1px 2px rgba(15,23,42,0.04), 0 1px 3px rgba(15,23,42,0.03); }
        button, input, select, .ims-clickable { transition: background .15s ease, border-color .15s ease, box-shadow .15s ease, color .15s ease, transform .1s ease; }
      `}</style>

      <datalist id="brand-list">{knownBrands.map(b => <option key={b} value={b} />)}</datalist>

      {/* Per-SKU history popover */}
      {skuHistory && (
        <div onClick={() => setSkuHistory(null)} style={{
          position: 'fixed', inset: 0, zIndex: 60, background: 'rgba(15,23,42,0.45)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '24px',
        }}>
          <div onClick={ev => ev.stopPropagation()} style={{
            background: 'white', borderRadius: '10px', width: '100%', maxWidth: '560px',
            maxHeight: '80vh', overflow: 'hidden', display: 'flex', flexDirection: 'column',
            boxShadow: '0 20px 50px rgba(0,0,0,0.3)',
          }}>
            <div style={{ padding: '16px 20px', borderBottom: '1px solid #E2E8F0', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div>
                <div style={{ fontSize: '13px', fontWeight: 700, color: C.ink }}>SKU history</div>
                <div style={{ fontSize: '13px', color: C.indigoInk, fontFamily: 'ui-monospace, monospace', fontWeight: 600 }}>{skuHistory.key}</div>
              </div>
              <button onClick={() => setSkuHistory(null)} style={{ background: 'none', border: '1px solid #E2E8F0', borderRadius: '6px', padding: '4px 10px', fontSize: '12px', cursor: 'pointer', color: C.muted }}>Close</button>
            </div>
            <div style={{ overflowY: 'auto', padding: '8px 0' }}>
              {skuHistory.events.length === 0 ? (
                <p style={{ fontSize: '12px', color: C.faint, padding: '16px 20px', margin: 0 }}>No history for this SKU.</p>
              ) : skuHistory.events.map(e => {
                const badge = ACTION_BADGE[e.action] ?? { label: e.action, bg: C.monoBg, color: C.sub }
                return (
                  <div key={e.id} style={{ padding: '10px 20px', borderBottom: '1px solid #F8FAFC', display: 'flex', gap: '10px', alignItems: 'baseline' }}>
                    <span style={{ fontSize: '10px', fontWeight: 700, background: badge.bg, color: badge.color, padding: '2px 7px', borderRadius: '99px', whiteSpace: 'nowrap' }}>{badge.label}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: '12px', color: C.ink }}>{auditSummary(e) || '—'}</div>
                      <div style={{ fontSize: '11px', color: C.faint, marginTop: '2px' }}>
                        {e.display_name ?? 'Unknown'} · {fmtWhen(e.created_at)}
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}

      <div style={{ maxWidth: '1060px' }}>

        {/* Header */}
        <div style={{ marginBottom: '18px', display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' }}>
          <div>
            <h1 style={{ fontSize: '20px', fontWeight: 700, color: C.ink }}>Catalogue Ingestion</h1>
            <p style={{ fontSize: '12px', color: C.faint, marginTop: '2px' }}>
              Upload supplier price lists · AI extracts products · Review and assign SKUs
            </p>
          </div>
          {imports.length > 0 && (pendingCount > 0 || can('catalogue_admin')) && (
            <div style={{ position: 'relative' }}>
              <button onClick={() => setManageOpen(o => !o)}
                style={{ background: 'white', color: C.sub, border: '1px solid #E2E8F0', borderRadius: '7px', padding: '7px 14px', fontSize: '12px', fontWeight: 600, cursor: 'pointer' }}>
                ⋯ Manage
              </button>
              {manageOpen && (
                <>
                  <div onClick={() => setManageOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 30 }} />
                  <div style={{ position: 'absolute', top: '38px', right: 0, zIndex: 31, background: 'white', border: '1px solid #E2E8F0', borderRadius: '9px', boxShadow: '0 8px 24px rgba(15,23,42,0.14)', minWidth: '230px', overflow: 'hidden', padding: '4px' }}>
                    {pendingCount > 0 && (
                      <button onClick={() => { setManageOpen(false); retagAll() }} disabled={retagging}
                        style={{ display: 'flex', width: '100%', textAlign: 'left', alignItems: 'center', gap: '8px', padding: '8px 10px', fontSize: '12px', color: C.indigoInk, background: 'none', border: 'none', borderRadius: '6px', cursor: 'pointer' }} className="ims-menu-item">
                        {retagging ? <><Spinner /> Tagging…</> : <>✨ Re-run AI tagging on {pendingCount} pending</>}
                      </button>
                    )}
                    {can('catalogue_admin') && pendingCount > 0 && (
                      <button onClick={() => { setManageOpen(false); removeQueuedItems() }} disabled={clearingPending}
                        style={{ display: 'flex', width: '100%', textAlign: 'left', alignItems: 'center', gap: '8px', padding: '8px 10px', fontSize: '12px', color: C.amberInk, background: 'none', border: 'none', borderRadius: '6px', cursor: 'pointer' }} className="ims-menu-item">
                        {clearingPending ? <><Spinner /> Removing…</> : `🧹 Remove ${pendingCount} queued items`}
                      </button>
                    )}
                    {can('catalogue_admin') && (
                      <button onClick={() => { setManageOpen(false); deleteAllCatalogues() }} disabled={deletingCatalogues}
                        style={{ display: 'flex', width: '100%', textAlign: 'left', alignItems: 'center', gap: '8px', padding: '8px 10px', fontSize: '12px', color: C.redInk, background: 'none', border: 'none', borderRadius: '6px', cursor: 'pointer' }} className="ims-menu-item">
                        {deletingCatalogues ? <><Spinner /> Deleting…</> : '🗑 Delete all catalogues'}
                      </button>
                    )}
                  </div>
                </>
              )}
            </div>
          )}
        </div>
        <style>{`.ims-menu-item:hover { background:#F5F3FF } .ims-menu-item:disabled { opacity:.6; cursor:default }`}</style>

        {/* ══ Unified upload — drop or pick (1+ files), supplier optional ════ */}
        <div style={{ background: 'white', border: '1px solid #E8EDF3', borderRadius: '12px', boxShadow: '0 1px 2px rgba(15,23,42,0.04)', padding: '18px', marginBottom: '22px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap', marginBottom: '12px' }}>
            <h2 style={{ fontSize: '13px', fontWeight: 700, color: C.ink, margin: 0 }}>Upload supplier catalogues</h2>
            <select
              value={batchSupplierId ?? ''} disabled={batchRunning}
              onChange={e => setBatchSupplier(e.target.value ? Number(e.target.value) : null)}
              title="Apply one supplier to every uploaded file (otherwise matched by folder name)"
              style={{ fontSize: '12px', border: '1px solid #E2E8F0', borderRadius: '6px', padding: '7px 8px', background: 'white', color: batchSupplierId ? C.indigoInk : C.muted, fontWeight: batchSupplierId ? 600 : 400, maxWidth: '220px' }}>
              <option value="">Supplier: auto (by folder)</option>
              {suppliers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>

          {/* Drop zone */}
          <div
            onDragOver={e => { e.preventDefault(); if (!batchRunning) setDragOver(true) }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
            style={{
              border: `2px dashed ${dragOver ? C.indigo : C.knobOff}`, borderRadius: '10px',
              background: dragOver ? C.primaryBg : C.wash, padding: '22px', textAlign: 'center',
              transition: 'all 0.12s',
            }}>
            <p style={{ fontSize: '13px', color: C.sub, margin: '0 0 10px', fontWeight: 500 }}>
              {dragOver ? 'Drop to upload' : 'Drag files here, or'}
            </p>
            <div style={{ display: 'inline-flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'center' }}>
              <label style={{ fontSize: '12px', fontWeight: 600, color: batchRunning ? C.knobOff : 'white', background: batchRunning ? C.line : C.indigo, borderRadius: '7px', padding: '8px 16px', cursor: batchRunning ? 'default' : 'pointer' }}>
                📄 Choose files
                <input ref={batchFilesInputRef} type="file" multiple disabled={batchRunning} onChange={handleBatchSelect} style={{ display: 'none' }} />
              </label>
              <label style={{ fontSize: '12px', fontWeight: 600, color: batchRunning ? C.knobOff : C.indigoInk, background: batchRunning ? C.monoBg : 'white', border: '1px solid #C7D2FE', borderRadius: '7px', padding: '8px 16px', cursor: batchRunning ? 'default' : 'pointer' }}
                title="Pick a whole Region/Supplier folder. Your browser shows its own 'Upload N files?' prompt for folders.">
                📁 Choose folder
                <input ref={batchInputRef} type="file" multiple disabled={batchRunning} onChange={handleBatchSelect} style={{ display: 'none' }} />
              </label>
            </div>
            <p style={{ fontSize: '11px', color: C.faint, marginTop: '10px' }}>
              PDF · Excel · CSV · JPG · PNG. Handles one file or a whole batch — files extract via AI 3 at a time while you keep reviewing.
            </p>
          </div>

          {/* Batch progress + per-file list (only once files are picked) */}
          {batchFiles.length > 0 && (
            <div style={{ marginTop: '14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', marginBottom: '10px' }}>
                {!batchRunning && batchStats.queued > 0 && (
                  <button onClick={runBatch} style={{ background: C.indigo, color: 'white', border: 'none', borderRadius: '6px', padding: '7px 18px', fontSize: '13px', fontWeight: 600, cursor: 'pointer' }}>
                    ▶ Start ({batchStats.queued}{batchStats.error > 0 ? ` + ${batchStats.error} retry` : ''})
                  </button>
                )}
                {!batchRunning && batchStats.queued === 0 && batchStats.error > 0 && (
                  <button onClick={retryAllFailed} style={{ background: '#F59E0B', color: 'white', border: 'none', borderRadius: '6px', padding: '7px 18px', fontSize: '13px', fontWeight: 600, cursor: 'pointer' }}>
                    ↻ Retry all failed ({batchStats.error})
                  </button>
                )}
                {batchRunning && (
                  <button onClick={cancelBatch} style={{ background: C.redBg, color: C.redInk, border: 'none', borderRadius: '6px', padding: '7px 18px', fontSize: '13px', fontWeight: 600, cursor: 'pointer' }}>■ Cancel</button>
                )}
                {!batchRunning && <Ghost onClick={clearBatch}>Clear</Ghost>}
                <span style={{ fontSize: '12px', color: C.muted, marginLeft: 'auto', display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
                  <span><strong>{batchStats.total}</strong> files</span>
                  <span style={{ color: '#16A34A' }}>{batchStats.done} done</span>
                  {batchStats.uploading > 0 && <span style={{ color: '#1E40AF' }}>{batchStats.uploading} extracting</span>}
                  {batchStats.queued > 0 && <span>{batchStats.queued} queued</span>}
                  {batchStats.error > 0 && <span style={{ color: C.redInk }}>{batchStats.error} failed</span>}
                  <span style={{ color: C.ink }}><strong>{batchStats.items}</strong> items</span>
                </span>
              </div>
              <div style={{ height: '6px', background: C.monoBg, borderRadius: '99px', overflow: 'hidden', marginBottom: '12px' }}>
                <div style={{ height: '100%', width: `${batchStats.total ? Math.round((batchStats.done + batchStats.error) / batchStats.total * 100) : 0}%`, background: C.indigo, transition: 'width 0.3s' }} />
              </div>
              <div style={{ maxHeight: '360px', overflowY: 'auto', border: '1px solid #F1F5F9', borderRadius: '6px' }}>
                {batchGroups.map(g => (
                  <div key={g.folder}>
                    <div style={{ position: 'sticky', top: 0, zIndex: 1, display: 'flex', alignItems: 'center', gap: '10px', padding: '7px 12px', background: C.wash, borderTop: '1px solid #E2E8F0', borderBottom: '1px solid #F1F5F9', fontSize: '12px', fontWeight: 600, color: C.ink }}>
                      <span style={{ flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={g.folder}>
                        {g.folder}{!g.matched && <span style={{ color: C.knobOff, fontWeight: 400 }}> · no supplier match</span>}
                      </span>
                      <span style={{ fontWeight: 400, color: C.muted }}>
                        {g.done}/{g.total} done{g.items ? ` · ${g.items} items` : ''}{g.error ? ` · ` : ''}
                        {g.error > 0 && <span style={{ color: C.redInk, fontWeight: 600 }}>{g.error} failed</span>}
                      </span>
                      {g.error > 0 && !batchRunning && (
                        <button onClick={() => retrySupplier(g.folder)} style={{ background: C.warnBg, color: C.amberInk, border: '1px solid #FDE68A', borderRadius: '5px', padding: '3px 10px', fontSize: '11px', fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap' }}>↻ Retry {g.error}</button>
                      )}
                    </div>
                    {g.files.map(b => {
                      const badge = BATCH_BADGE[b.status]
                      const meta: string[] = []
                      if (b.sizeMB >= 0.1) meta.push(`${b.sizeMB.toFixed(1)} MB`)
                      if (b.status === 'uploading') meta.push('extracting…')
                      if (b.status === 'done') {
                        if (b.fmt) meta.push(b.fmt.toUpperCase())
                        if (b.detectedSupplier) meta.push(`supplier: ${b.detectedSupplier}`)
                        if (b.detectedBrands) meta.push(`brand: ${b.detectedBrands}`)
                        if (b.supplierStatus === 'needs_review') meta.push('⚠ confirm supplier')
                      }
                      return (
                        <div key={b.key} style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '7px 12px', borderTop: '1px solid #F1F5F9', fontSize: '12px' }}>
                          <span style={{ flex: '0 0 84px', fontSize: '10px', fontWeight: 700, textAlign: 'center', background: badge.bg, color: badge.color, padding: '2px 6px', borderRadius: '99px', marginTop: '1px' }}>
                            {b.status === 'uploading' ? '◷ extracting' : badge.label}
                          </span>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ color: C.ink, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={b.name}>{b.name}</div>
                            {meta.length > 0 && (
                              <div style={{ fontSize: '10.5px', color: b.status === 'uploading' ? '#1E40AF' : C.faint, marginTop: '2px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={meta.join(' · ')}>{meta.join(' · ')}</div>
                            )}
                          </div>
                          <span style={{ flex: '0 0 auto', color: b.status === 'error' ? C.redInk : '#16A34A', fontWeight: 600, maxWidth: '160px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', marginTop: '1px' }} title={b.error ?? ''}>
                            {b.status === 'done' ? `${b.itemCount} items` : b.status === 'error' ? (b.error ?? 'error') : ''}
                          </span>
                          {!batchRunning && (b.status === 'error' || b.status === 'done') && (
                            <button onClick={() => redoFile(b.key)} style={{ flex: '0 0 auto', background: 'none', border: '1px solid #E2E8F0', borderRadius: '5px', padding: '2px 8px', fontSize: '11px', fontWeight: 600, color: b.status === 'error' ? C.amberInk : C.muted, cursor: 'pointer', marginTop: '1px' }} title={b.status === 'error' ? 'Retry this file' : 'Re-extract this file (creates a new import)'}>
                              ↻ {b.status === 'error' ? 'Retry' : 'Redo'}
                            </button>
                          )}
                        </div>
                      )
                    })}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* ══ Resume an interrupted batch (recovered after a refresh) ═══════ */}
        {resumeSnap && batchFiles.length === 0 && (() => {
          const done = resumeSnap.files.filter(f => f.status === 'done').length
          const remaining = resumeSnap.files.length - done
          return (
            <div style={{ background: '#FFFBEB', border: '1px solid #FCD34D', borderRadius: '12px', padding: '14px 18px', marginBottom: '16px', display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap' }}>
              <span style={{ fontSize: '20px' }}>⏸</span>
              <div style={{ flex: 1, minWidth: '240px' }}>
                <div style={{ fontSize: '13px', fontWeight: 700, color: C.amberInk }}>
                  Unfinished batch from {new Date(resumeSnap.savedAt).toLocaleString()}
                </div>
                <div style={{ fontSize: '12px', color: '#78350F', marginTop: '2px' }}>
                  {done} of {resumeSnap.files.length} already scanned · <strong>{remaining} left</strong>.
                  Re-pick the same files/folder to continue — already-scanned files are skipped automatically.
                </div>
              </div>
              <label style={{ fontSize: '12px', fontWeight: 600, color: C.indigoInk, background: 'white', border: '1px solid #C7D2FE', borderRadius: '6px', padding: '7px 14px', cursor: 'pointer', whiteSpace: 'nowrap' }}>
                📄 Re-pick files
                <input type="file" multiple onChange={handleBatchSelect} style={{ display: 'none' }} />
              </label>
              <button onClick={discardResume}
                style={{ fontSize: '12px', fontWeight: 600, color: C.amberInk, background: 'none', border: '1px solid #FDE68A', borderRadius: '6px', padding: '7px 12px', cursor: 'pointer', whiteSpace: 'nowrap' }}>
                Discard
              </button>
            </div>
          )
        })()}

        {/* Review queue — Xero-style reconciliation list */}
        <div style={{ marginBottom: '28px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '14px', flexWrap: 'wrap' }}>
            <h2 style={{ fontSize: '15px', fontWeight: 600, color: C.ink, margin: 0 }}>Reconcile Catalogue → IMS</h2>
            {/* Active review queue · Skipped bucket · Confirmed list */}
            <div style={{ display: 'inline-flex', background: C.monoBg, borderRadius: '7px', padding: '2px' }}>
              {([
                ['review',    '📋 Review',    pendingCount,   C.ink],
                ['skipped',   '⏭ Skipped',   skippedCount,   C.amberInk],
                ['confirmed', '✓ Confirmed', confirmedCount, C.green],
              ] as const).map(([k, lbl, count, activeColor]) => {
                const active = view === k
                return (
                  <button key={k} onClick={() => setView(k)}
                    style={{ padding: '4px 11px', fontSize: '12px', fontWeight: 600, border: 'none', borderRadius: '5px', cursor: 'pointer', whiteSpace: 'nowrap', background: active ? 'white' : 'transparent', color: active ? activeColor : C.muted, boxShadow: active ? '0 1px 2px rgba(0,0,0,0.08)' : 'none' }}>
                    {lbl} {count ? `(${count})` : ''}
                  </button>
                )
              })}
            </div>
            {/* Scope: one dropdown replaces the wall of per-import pills */}
            {imports.length > 1 && (
              <select value={selectedImportId ?? ''} onChange={e => setSelectedImportId(e.target.value ? Number(e.target.value) : null)}
                title="Scope the queue to one scanned file (loads all of its items)"
                style={{ border: '1px solid #E2E8F0', borderRadius: '7px', padding: '5px 10px', fontSize: '12px', background: 'white', color: selectedImportId != null ? C.indigoInk : C.sub, fontWeight: selectedImportId != null ? 600 : 400, maxWidth: '260px' }}>
                <option value="">All imports ({queue.length})</option>
                {imports.filter(i => i.counts.pending > 0).map(imp => (
                  <option key={imp.id} value={imp.id}>{imp.supplier_name ?? imp.filename.slice(0, 36)} ({imp.counts.pending})</option>
                ))}
              </select>
            )}
            <span style={{ fontSize: '12px', color: C.faint, marginLeft: 'auto' }}>
              {view === 'confirmed' ? `${confirmed.length} item${confirmed.length === 1 ? '' : 's'}` : (
                <>
                  {visibleItems.length === itemsWithTier.length ? `${itemsWithTier.length} items` : `${visibleItems.length} of ${itemsWithTier.length} items`}
                  {selectedImportId == null && queueTotal > queue.length && (
                    <> · <button onClick={() => fetchQueue(queue.length + QUEUE_PAGE)}
                      style={{ background: 'none', border: 'none', color: C.indigo, fontSize: '12px', fontWeight: 600, cursor: 'pointer', padding: 0 }}>
                      load {Math.min(QUEUE_PAGE, queueTotal - queue.length)} more of {queueTotal}</button></>
                  )}
                </>
              )}
            </span>
          </div>

          {/* ════════ REVIEW view — the card-based reconciliation queue ════════ */}
          {view === 'review' && (<>

          {/* ⚠ Already-verified detector — a loud, reviewable banner for re-upload duplicates */}
          {alreadyVerified.count > 0 && (
            <div style={{ marginBottom: '14px', border: '2px solid #FB923C', borderRadius: '12px', overflow: 'hidden', boxShadow: '0 2px 14px rgba(234,88,12,0.25)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '14px 18px', background: 'linear-gradient(90deg,#FFEDD5,#FFF7ED)' }}>
                <span style={{ fontSize: '22px' }}>🔁</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '14px', fontWeight: 800, color: '#9A3412' }}>
                    {alreadyVerified.count} already-verified SKU{alreadyVerified.count === 1 ? '' : 's'} detected in this scan
                  </div>
                  <div style={{ fontSize: '12px', color: '#9A3412', opacity: 0.9, marginTop: '2px' }}>
                    These re-match products you’ve already onboarded (a re-upload). Skipping them marks each <strong>verified</strong> — they move to <strong>Confirmed</strong>, not the Skipped bucket — so you never re-review them.
                  </div>
                </div>
                <button onClick={() => setShowAlreadyVerified(s => !s)}
                  style={{ flexShrink: 0, padding: '8px 16px', fontSize: '13px', fontWeight: 700, background: '#EA580C', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
                  {showAlreadyVerified ? 'Hide' : 'Review & skip →'}
                </button>
              </div>
              {showAlreadyVerified && (
                <div style={{ background: 'white' }}>
                  <div style={{ maxHeight: '320px', overflowY: 'auto' }}>
                    {alreadyVerified.items.map((it) => (
                      <div key={it.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 18px', borderTop: '1px solid #F1F5F9', fontSize: '12.5px' }}>
                        <span style={{ flex: 1, minWidth: 0, color: C.ink, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.raw_description || '—'}</span>
                        <span style={{ color: C.faint, flexShrink: 0 }}>→</span>
                        <a href={`/items/${skuToPath(it.matched_sku)}`} target="_blank" rel="noopener noreferrer"
                          title={it.matched_name || ''}
                          style={{ color: C.indigo, fontWeight: 600, textDecoration: 'none', fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}>{it.matched_sku} ↗</a>
                        <span style={{ flexShrink: 0, fontSize: '11px', fontWeight: 700, padding: '1px 7px', borderRadius: '999px', background: C.greenBg, color: C.green, fontVariantNumeric: 'tabular-nums' }}>{Math.round(it.confidence * 100)}%</span>
                        <span style={{ flexShrink: 0, fontSize: '10px', color: C.faint }}>{it.match_type}</span>
                      </div>
                    ))}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', padding: '12px 18px', borderTop: '1px solid #FDBA74', background: '#FFF7ED' }}>
                    <span style={{ fontSize: '11.5px', color: '#9A3412' }}>Reversible — undo any from the Confirmed list.</span>
                    <button onClick={doSkipAlreadyVerified} disabled={skippingVerified}
                      style={{ flexShrink: 0, padding: '9px 18px', fontSize: '13px', fontWeight: 700, background: skippingVerified ? C.knobOff : '#EA580C', color: 'white', border: 'none', borderRadius: '8px', cursor: skippingVerified ? 'default' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: '7px' }}>
                      {skippingVerified ? <><Spinner /> Skipping…</> : `✓ Skip all ${alreadyVerified.count} as already-verified`}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Search scanned products */}
          {(itemsWithTier.length > 0 || anyQueueFilter) && (
            <div style={{ position: 'relative', marginBottom: '12px' }}>
              <span style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)', fontSize: '13px', color: C.faint, pointerEvents: 'none' }}>🔍</span>
              <input
                value={itemSearch}
                onChange={e => setItemSearch(e.target.value)}
                placeholder="Search scanned products — name, SKU, barcode, brand, tag, subcategory…"
                style={{ width: '100%', border: '1px solid #E2E8F0', borderRadius: '10px', padding: '10px 36px 10px 34px', fontSize: '13px', background: 'white', boxShadow: '0 1px 2px rgba(15,23,42,0.04)' }}
              />
              {itemSearch && (
                <button onClick={() => setItemSearch('')} title="Clear"
                  style={{ position: 'absolute', right: '10px', top: '50%', transform: 'translateY(-50%)', border: 'none', background: C.monoBg, color: C.muted, borderRadius: '50%', width: '20px', height: '20px', fontSize: '12px', cursor: 'pointer', lineHeight: 1 }}>×</button>
              )}
            </div>
          )}

          {loading && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', padding: '4px 0' }}>
              {[0, 1, 2].map(i => <div key={i} className="ims-skeleton" style={{ height: '92px', borderRadius: '10px' }} />)}
            </div>
          )}

          {!loading && itemsWithTier.length === 0 && (
            <div style={{ background: 'white', border: '1px solid #E8EDF3', borderRadius: '12px', boxShadow: '0 1px 2px rgba(15,23,42,0.04)', padding: '36px', textAlign: 'center', color: C.faint, fontSize: '13px' }}>
              {anyQueueFilter
                ? <>No items match your search or filters. <button onClick={() => { setItemSearch(''); setSupplierFilter(''); setBrandFilter(''); setFilter('all') }} style={{ border: 'none', background: 'none', color: C.indigo, fontWeight: 600, cursor: 'pointer', padding: 0, fontSize: '13px' }}>Clear all</button> to see the queue.</>
                : 'No items pending review. Upload a supplier catalogue above to get started.'}
            </div>
          )}

          {/* ── Stage 1: confirm the supplier for this catalogue (per file) ── */}
          {selectedImport && (() => {
            const confPct = selectedImport.supplier_confidence != null
              ? Math.round(selectedImport.supplier_confidence * 100) : null
            const isConfirmed = selectedImport.supplier_status === 'confirmed'
            return (
              <div style={{
                background: isConfirmed ? 'linear-gradient(90deg,#DCFCE7,#ECFDF5)' : 'linear-gradient(90deg,#FEF3C7,#FFFBEB)',
                border: `1px solid ${isConfirmed ? '#86EFAC' : '#FCD34D'}`,
                borderRadius: '8px', padding: '14px 18px', marginBottom: '14px',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                  <span style={{ fontSize: '12px', fontWeight: 700, color: isConfirmed ? C.green : C.amberInk }}>
                    {isConfirmed ? '✓ Supplier confirmed' : '① Confirm the supplier for this catalogue'}
                  </span>
                  {selectedImport.detected_supplier_name && (
                    <span style={{ fontSize: '11.5px', color: C.sub }}>
                      AI detected: <strong>{selectedImport.detected_supplier_name}</strong>{confPct != null ? ` (${confPct}%)` : ''}
                    </span>
                  )}
                  {selectedImport.detected_brands && (
                    <span style={{ fontSize: '11px', color: C.muted }}>brands: {selectedImport.detected_brands}</span>
                  )}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '10px', flexWrap: 'wrap' }}>
                  <select
                    value={supplierChoice}
                    onChange={e => setSupplierChoice(e.target.value ? Number(e.target.value) : '')}
                    style={{ padding: '7px 10px', border: '1px solid #CBD5E1', borderRadius: '6px', fontSize: '13px', minWidth: '260px', background: 'white' }}
                  >
                    <option value="">— select supplier —</option>
                    {suppliers.map(s => (
                      <option key={s.id} value={s.id}>{s.name}{s.code ? ` (${s.code})` : ''}</option>
                    ))}
                  </select>
                  <button
                    onClick={() => supplierChoice && confirmSupplier(selectedImport.id, Number(supplierChoice))}
                    disabled={!supplierChoice || confirmingSupplier || (isConfirmed && supplierChoice === selectedImport.supplier_id)}
                    style={{
                      background: (!supplierChoice || confirmingSupplier || (isConfirmed && supplierChoice === selectedImport.supplier_id)) ? C.knobOff : '#16A34A',
                      color: 'white', border: 'none', borderRadius: '6px', padding: '7px 16px',
                      fontSize: '13px', fontWeight: 700,
                      cursor: (!supplierChoice || confirmingSupplier) ? 'default' : 'pointer',
                      display: 'inline-flex', alignItems: 'center', gap: '7px',
                    }}
                  >
                    {confirmingSupplier ? <><Spinner /> Saving…</> : isConfirmed ? 'Update supplier' : '✓ Confirm supplier'}
                  </button>
                  {selectedImport.supplier_name && (
                    <span style={{ fontSize: '11.5px', color: isConfirmed ? C.green : C.amberInk }}>
                      Current: <strong>{selectedImport.supplier_name}</strong>
                      {selectedImport.supplier_segment ? ` · ${selectedImport.supplier_segment}` : ''}
                    </span>
                  )}
                </div>
                {needsSupplierConfirm && (
                  <p style={{ fontSize: '11.5px', color: C.amberInk, margin: '10px 0 0' }}>
                    SKU review is locked until you confirm the supplier — every item in this file is assigned to it.
                  </p>
                )}
              </div>
            )
          })()}

          {/* ── Sally-style queue progress strip ─────────────────────────── */}
          {itemsWithTier.length > 0 && (() => {
            const importsScoped = selectedImportId == null
              ? imports
              : imports.filter(i => i.id === selectedImportId)
            const totals = importsScoped.reduce((acc, i) => ({
              total:    acc.total    + (i.item_count ?? 0),
              pending:  acc.pending  + (i.counts.pending  ?? 0),
              matched:  acc.matched  + (i.counts.matched  ?? 0),
              new_sku:  acc.new_sku  + (i.counts.new_sku  ?? 0),
              rejected: acc.rejected + (i.counts.rejected ?? 0),
            }), { total: 0, pending: 0, matched: 0, new_sku: 0, rejected: 0 })
            const reviewed = totals.matched + totals.new_sku + totals.rejected
            const pct = totals.total > 0 ? Math.round(reviewed / totals.total * 100) : 0
            return (
              <div style={{
                background: C.ink, color: C.line, borderRadius: '10px',
                padding: '12px 16px', marginBottom: '16px',
                display: 'flex', alignItems: 'center', gap: '14px', fontSize: '12px', flexWrap: 'wrap',
              }}>
                <strong style={{ color: C.wash, fontSize: '13px' }}>Queue progress</strong>
                <span style={{ background: '#14532D', border: '1px solid #166534', color: '#86EFAC', padding: '3px 9px', borderRadius: '99px', fontWeight: 600 }}>
                  {totals.matched} matched
                </span>
                <span style={{ background: '#3B0764', border: '1px solid #6B21A8', color: '#D8B4FE', padding: '3px 9px', borderRadius: '99px', fontWeight: 600 }}>
                  {totals.new_sku} new SKU
                </span>
                <span style={{ background: '#450A0A', border: '1px solid #7F1D1D', color: '#FCA5A5', padding: '3px 9px', borderRadius: '99px', fontWeight: 600 }}>
                  {totals.rejected} rejected
                </span>
                <span style={{ background: '#1E293B', border: '1px solid #334155', color: C.knobOff, padding: '3px 9px', borderRadius: '99px', fontWeight: 600 }}>
                  {totals.pending} pending
                </span>
                <div style={{ flex: 1, minWidth: '200px', height: '8px', background: '#1E293B', borderRadius: '99px', overflow: 'hidden' }}>
                  <div style={{
                    width: `${pct}%`,
                    height: '100%',
                    background: 'linear-gradient(to right, #6366F1, #818CF8)',
                  }} />
                </div>
                <span style={{ color: C.faint, fontVariantNumeric: 'tabular-nums' }}>
                  {reviewed} of {totals.total} reviewed ({pct}%)
                </span>
              </div>
            )
          })()}

          {/* ── Quick-wins banner (Tier 1: 95%+ confidence) ─────────────── */}
          {(tierCounts.t1a + tierCounts.t1b) > 0 && (
            <div style={{
              background: 'linear-gradient(90deg, #DCFCE7 0%, #ECFDF5 100%)',
              border: '1px solid #86EFAC',
              borderRadius: '8px',
              padding: '14px 18px',
              marginBottom: '14px',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '16px',
              flexWrap: 'wrap',
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <p style={{ fontSize: '13px', fontWeight: 700, color: C.green, margin: 0 }}>
                  ✨ {tierCounts.t1a + tierCounts.t1b} item{(tierCounts.t1a + tierCounts.t1b) === 1 ? '' : 's'} ready to reconcile ({tierCounts.t1a} at 99%+, {tierCounts.t1b} at 95-98%)
                </p>
                <p style={{ fontSize: '11.5px', color: C.ok, marginTop: '3px' }}>
                  These matched at 95%+ confidence — same SKU, name + cost agree. Equivalent to <code>OK</code> on a Xero bank statement line.
                </p>
                <p style={{ fontSize: '11px', color: '#16A34A', marginTop: '3px', display: 'flex', alignItems: 'center', gap: '5px' }}>
                  <span style={{ fontWeight: 700, color: C.indigoInk }}>✨ AI tags</span>
                  <span style={{ color: C.muted }}>
                    confirmed per item below are applied on reconcile ({t1ItemsInScope.reduce((n, i) => n + effectiveTags(getAction(i.id), i).length, 0)} tags across {t1ItemsInScope.length} items)
                  </span>
                </p>
              </div>
              <div style={{ display: 'flex', gap: '8px' }}>
                <button
                  onClick={() => setFilter('t1')}
                  style={{
                    background: 'white', color: C.green, border: '1px solid #86EFAC',
                    borderRadius: '6px', padding: '7px 14px',
                    fontSize: '12px', fontWeight: 600, cursor: 'pointer',
                  }}
                >
                  Review list
                </button>
                <button
                  onClick={doBulkApproveTier1}
                  disabled={bulkBusy === 'match'}
                  style={{
                    background: '#16A34A', color: 'white', border: 'none',
                    borderRadius: '6px', padding: '7px 16px',
                    fontSize: '13px', fontWeight: 700, cursor: bulkBusy === 'match' ? 'default' : 'pointer',
                    opacity: bulkBusy === 'match' ? 0.6 : 1,
                  }}
                  title="Matches every pending item with a ≥95% match across the entire queue, not just the loaded page"
                >
                  {bulkBusy === 'match' ? 'Reconciling…' : '✓ Reconcile all high-confidence'}
                </button>
              </div>
            </div>
          )}

          {/* ── Filters — 5 clear states, with brand/supplier/inactive in "More" ── */}
          {(itemsWithTier.length > 0 || anyQueueFilter) && (
            <div style={{ background: 'white', border: '1px solid #E8EDF3', borderRadius: '12px', boxShadow: '0 1px 2px rgba(15,23,42,0.04)', padding: '10px 14px', marginBottom: '10px' }}>
              <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', alignItems: 'center' }}>
                {([
                  ['all', `All (${itemsWithTier.length})`, C.sub],
                  ['t1',  `Strong ≥95% (${tierCounts.t1a + tierCounts.t1b})`, '#16A34A'],
                  ['t2',  `Needs review (${tierCounts.t2a + tierCounts.t2b})`, '#2563EB'],
                  ['t4',  `No match · carried (${tierCounts.t4})`, '#9333EA'],
                  ['t3',  `Likely reject (${tierCounts.t3})`, '#DC2626'],
                ] as const).map(([key, label, color]) => (
                  <button key={key} onClick={() => { setFilter(key); clearSelection() }}
                    style={{ background: filter === key ? color : C.monoBg, color: filter === key ? 'white' : C.sub, border: 'none', borderRadius: '99px', padding: '5px 12px', fontSize: '12px', fontWeight: 600, cursor: 'pointer' }}>
                    {label}
                  </button>
                ))}

                {/* Active brand/supplier filter shown as a removable chip */}
                {(brandFilter || supplierFilter) && (
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '11px', fontWeight: 600, background: C.primaryBg, color: C.indigoInk, borderRadius: '99px', padding: '3px 6px 3px 10px' }}>
                    {brandFilter || suppliers.find(s => String(s.id) === supplierFilter)?.name}
                    <button onClick={() => { setBrandFilter(''); setSupplierFilter('') }} style={{ border: 'none', background: 'none', color: C.indigo, cursor: 'pointer', fontSize: '13px', lineHeight: 1 }}>×</button>
                  </span>
                )}

                <div style={{ marginLeft: 'auto', position: 'relative', display: 'flex', gap: '8px', alignItems: 'center' }}>
                  {includeInactive && <span style={{ fontSize: '10.5px', color: C.amberInk }}>incl. inactive SKUs</span>}
                  <button onClick={() => setAdvancedOpen(o => !o)}
                    style={{ background: advancedOpen ? C.primaryBg : 'white', color: C.sub, border: '1px solid #E2E8F0', borderRadius: '7px', padding: '5px 12px', fontSize: '12px', fontWeight: 600, cursor: 'pointer' }}>
                    ⚙ More
                  </button>
                  {advancedOpen && (
                    <>
                      <div onClick={() => setAdvancedOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 20 }} />
                      <div style={{ position: 'absolute', top: '34px', right: 0, zIndex: 21, background: 'white', border: '1px solid #E2E8F0', borderRadius: '9px', boxShadow: '0 8px 24px rgba(15,23,42,0.14)', padding: '12px', width: '260px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
                        <label style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                          <span style={{ fontSize: '10px', fontWeight: 700, color: C.faint, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Brand</span>
                          <select value={brandFilter} onChange={e => { setBrandFilter(e.target.value); clearSelection() }} style={{ border: '1px solid #E2E8F0', borderRadius: '6px', padding: '6px 8px', fontSize: '12px', background: 'white' }}>
                            <option value="">All brands</option>
                            {allBrandsInQueue.map(b => {
                              const inDb = brandsInDb.has(b.toLowerCase().trim())
                              return <option key={b} value={b}>{inDb ? '✓ ' : '✗ '}{b} ({itemsWithTier.filter(i => i.brand === b).length})</option>
                            })}
                          </select>
                        </label>
                        {brandFilter && (
                          <button onClick={() => { doRejectBrand(brandFilter); setAdvancedOpen(false) }} disabled={bulkBusy === 'reject-brand'}
                            style={{ background: C.redBg, color: C.redInk, border: 'none', borderRadius: '6px', padding: '6px 10px', fontSize: '11.5px', fontWeight: 600, cursor: 'pointer' }}>
                            {bulkBusy === 'reject-brand' ? 'Rejecting…' : `✗ Reject all unmatched “${brandFilter}”`}
                          </button>
                        )}
                        <label style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                          <span style={{ fontSize: '10px', fontWeight: 700, color: C.faint, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Supplier</span>
                          <select value={supplierFilter} onChange={e => { setSupplierFilter(e.target.value); clearSelection() }} style={{ border: '1px solid #E2E8F0', borderRadius: '6px', padding: '6px 8px', fontSize: '12px', background: 'white' }}>
                            <option value="">All suppliers</option>
                            {supplierFacets.filter(f => f.supplier_id != null).map(f =>
                              <option key={f.supplier_id} value={String(f.supplier_id)}>{suppliers.find(s => s.id === f.supplier_id)?.name ?? `#${f.supplier_id}`} ({f.count})</option>)}
                          </select>
                        </label>
                        <label style={{ display: 'flex', alignItems: 'center', gap: '7px', fontSize: '12px', color: C.sub, cursor: 'pointer' }}
                          title="Also match against INACTIVE / DISCONTINUED SKUs — matching one revives it instead of creating a duplicate.">
                          <input type="checkbox" checked={includeInactive} onChange={e => { setIncludeInactive(e.target.checked); clearSelection() }} />
                          Match against inactive SKUs
                        </label>
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* ── Sticky bulk-action bar ───────────────────────────────────── */}
          {selectedIds.size > 0 && (
            <div style={{
              position: 'sticky', top: '12px', zIndex: 5,
              background: C.ink, color: 'white',
              borderRadius: '8px', padding: '10px 16px',
              display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '10px',
              boxShadow: '0 4px 14px rgba(15,23,42,0.18)',
            }}>
              <span style={{ fontSize: '12px', fontWeight: 700 }}>{selectedIds.size} selected</span>
              <button
                onClick={doBulkMatchSelected}
                disabled={!!bulkBusy}
                title="Confirmed AI tags on each selected item are applied"
                style={{ background: bulkBusy ? '#334155' : '#16A34A', color: 'white', border: 'none', borderRadius: '5px', padding: '5px 12px', fontSize: '11px', fontWeight: 700, cursor: bulkBusy ? 'default' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: '6px', opacity: bulkBusy && bulkBusy !== 'match' ? 0.5 : 1 }}
              >
                {bulkBusy === 'match' ? <><Spinner size={11} color="white" /> Matching…</> : '✓ Match to suggested SKU'}
              </button>
              <span style={{ fontSize: '10px', fontWeight: 700, color: C.indigoInk, background: C.primaryBg, borderRadius: '99px', padding: '2px 8px' }}>
                ✨ + AI tags
              </span>
              <select value={bulkRejectReason} onChange={e => setBulkRejectReason(e.target.value)} disabled={!!bulkBusy}
                title="Reason applied to the rejected items"
                style={{ background: '#1E293B', color: C.line, border: '1px solid #334155', borderRadius: '5px', padding: '5px 8px', fontSize: '11px', fontWeight: 600 }}>
                <option value="clinical_consumable">Clinical consumable</option>
                <option value="duplicate">Duplicate</option>
                <option value="out_of_scope">Out of scope</option>
                <option value="discontinued">Discontinued</option>
              </select>
              <button
                onClick={() => doBulkRejectSelected(bulkRejectReason)}
                disabled={!!bulkBusy}
                style={{ background: bulkBusy ? '#334155' : '#DC2626', color: 'white', border: 'none', borderRadius: '5px', padding: '5px 12px', fontSize: '11px', fontWeight: 700, cursor: bulkBusy ? 'default' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: '6px', opacity: bulkBusy && bulkBusy !== 'reject' ? 0.5 : 1 }}
              >
                {bulkBusy === 'reject' ? <><Spinner size={11} color="white" /> Rejecting…</> : '✗ Reject'}
              </button>
              <span style={{ marginLeft: 'auto' }}>
                <button
                  onClick={clearSelection}
                  style={{ background: 'transparent', color: C.faint, border: 'none', fontSize: '11px', fontWeight: 600, cursor: 'pointer' }}
                >
                  Clear
                </button>
              </span>
            </div>
          )}

          {/* ── Reconciliation list — v3 side-by-side diff cards ─────────── */}
          {visibleItems.length > 0 && (
            <>
              {/* Legend strip */}
              <div style={{ marginBottom: '10px', padding: '8px 12px', background: 'white', border: '1px solid #E2E8F0', borderRadius: '6px', fontSize: '11px', color: C.sub, display: 'flex', gap: '14px', alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={{ fontWeight: 700, color: C.faint, textTransform: 'uppercase', letterSpacing: '0.05em', fontSize: '10px' }}>Legend</span>
                <span><span style={{ display: 'inline-block', width: '10px', height: '10px', background: '#22C55E', borderRadius: '2px', verticalAlign: 'middle', marginRight: '5px' }}></span>all fields agree</span>
                <span><span style={{ display: 'inline-block', width: '10px', height: '10px', background: '#FCA5A5', borderRadius: '2px', verticalAlign: 'middle', marginRight: '5px' }}></span>field differs</span>
                <span><span style={{ display: 'inline-block', width: '10px', height: '10px', background: '#FDE68A', borderRadius: '2px', verticalAlign: 'middle', marginRight: '5px' }}></span>no match — context shown</span>
                <span style={{ marginLeft: 'auto' }}>
                  <label style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', cursor: 'pointer' }}>
                    <input
                      type="checkbox"
                      checked={visibleItems.length > 0 && visibleItems.every(i => selectedIds.has(i.id))}
                      onChange={e => e.target.checked ? selectAll(visibleItems.map(i => i.id)) : clearSelection()}
                    />
                    <span style={{ fontSize: '11px', color: C.sub, fontWeight: 600 }}>Select all visible</span>
                  </label>
                </span>
              </div>

              {visibleItems.length === 0 && (
                <div className="ims-card ims-fade-in" style={{ padding: '30px', textAlign: 'center', color: C.faint, fontSize: '13px' }}>
                  No scanned products match {itemSearch ? <strong style={{ color: C.sub }}>“{itemSearch}”</strong> : 'these filters'}.
                  <button onClick={() => { setItemSearch(''); setFilter('all'); setBrandFilter(''); setSupplierFilter('') }}
                    style={{ marginLeft: '10px', border: '1px solid #E2E8F0', background: 'white', borderRadius: '6px', padding: '4px 12px', fontSize: '12px', fontWeight: 600, color: C.sub, cursor: 'pointer' }}>
                    Clear search &amp; filters
                  </button>
                </div>
              )}

              {shownItems.map(item => {
                const a    = getAction(item.id)
                // A manually-picked SKU (Find & Match) overrides the AI suggestion for display
                // + confirm — picking no longer auto-confirms; the reviewer reviews then confirms.
                const top = a.pickedMatch ?? item.suggested_matches?.[0]
                const diff = top ? computeDiff(item, top) : null
                const isSelected = selectedIds.has(item.id)
                const busy = processing.has(item.id)

                // Frame color comes from the diff grade for matched rows; "no match" rows use a yellow strip
                const frameColor =
                  !top ? '#FDE68A' :
                  diff?.match_grade === 'perfect' ? '#22C55E' :
                  diff?.match_grade === 'partial' ? '#FED7AA' : '#FCA5A5'
                const stripBg =
                  !top ? '#FFFBEB' :
                  diff?.match_grade === 'perfect' ? '#F0FDF4' :
                  diff?.match_grade === 'partial' ? '#FFF7ED' : C.badBg
                const stripText =
                  !top ? C.amberInk :
                  diff?.match_grade === 'perfect' ? C.ok :
                  diff?.match_grade === 'partial' ? '#9A3412' : C.redInk

                // No-match context label
                const noMatchLabel =
                  item.tier === 't3' ? `Brand "${item.brand}" not in your IMS` :
                  item.tier === 't4' ? `No exact match — brand carried in IMS` :
                  'No match found'

                return (
                  <div key={item.id} className="ims-fade-in" style={{
                    position: 'relative',
                    marginBottom: '14px',
                    background: 'white',
                    border: `${top && diff?.match_grade === 'perfect' ? '2px' : '1px'} solid ${isSelected ? C.indigo : frameColor}`,
                    borderRadius: '12px',
                    overflow: 'hidden',
                    boxShadow: '0 1px 2px rgba(15,23,42,0.04), 0 2px 8px rgba(15,23,42,0.03)',
                    opacity: busy ? 0.85 : 1,
                    transition: 'opacity 0.15s',
                  }}>

                    {/* Indeterminate progress bar while a blocking action runs on this item */}
                    {busy && (
                      <div aria-hidden style={{ position: 'absolute', top: 0, left: 0, right: 0, height: '3px', background: C.line, overflow: 'hidden', zIndex: 3 }}>
                        <div style={{ position: 'absolute', top: 0, height: '100%', width: '40%', background: C.indigo, borderRadius: '2px', animation: 'ims-bar 1.1s ease-in-out infinite' }} />
                      </div>
                    )}

                    {/* Score breakdown strip */}
                    <div style={{
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      padding: '8px 14px', background: stripBg,
                      borderBottom: `1px solid ${frameColor}`,
                      fontSize: '11px', gap: '12px', flexWrap: 'wrap',
                    }}>
                      <div style={{ display: 'flex', gap: '14px', alignItems: 'center', flexWrap: 'wrap' }}>
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggleSelect(item.id)}
                          style={{ cursor: 'pointer' }}
                        />
                        <span style={{ fontWeight: 700, color: stripText, letterSpacing: '0.04em' }}>
                          {!top ? '⚠ NO MATCH' :
                           diff?.match_grade === 'perfect' ? `✓ CONFIDENT MATCH ${Math.round(top.confidence * 100)}%` :
                           diff?.match_grade === 'partial' ? `⚠ PARTIAL MATCH ${Math.round(top.confidence * 100)}%` :
                           `✗ WEAK MATCH ${Math.round(top.confidence * 100)}%`}
                        </span>
                        {diff && (
                          <>
                            <span style={{ color: C.sub }}>name <b style={{ color: diff.fields.name.ok ? C.ok : C.bad }}>{diff.fields.name.label}</b></span>
                            <span style={{ color: C.sub }}>brand <span style={{ color: diff.fields.brand.ok ? C.ok : C.bad, fontWeight: 600 }}>{diff.fields.brand.ok ? '✓' : '×'} {diff.fields.brand.label}</span></span>
                            <span style={{ color: C.sub }}>pack <span style={{ color: diff.fields.pack.ok ? C.ok : C.bad, fontWeight: 600 }}>{diff.fields.pack.ok ? '✓' : '×'} {diff.fields.pack.label}</span></span>
                            <span style={{ color: C.sub }}>cost <span style={{ color: diff.fields.cost.ok ? C.ok : C.bad, fontWeight: 600 }}>{diff.fields.cost.ok ? '✓' : '×'} {diff.fields.cost.label}</span></span>
                          </>
                        )}
                      </div>
                      <div style={{ color: stripText, fontWeight: 600 }}>
                        {!top ? noMatchLabel :
                         diff?.diff_count === 0 ? 'All fields agree' :
                         diff?.diff_count === 1 ? '1 field differs' :
                         `${diff?.diff_count} fields differ — review`}
                      </div>
                    </div>

                    {/* Two-column body */}
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 36px 1fr' }}>

                      {/* LEFT: extracted from PDF */}
                      <div style={{ padding: '12px 16px' }}>
                        <div style={{ fontSize: '10px', letterSpacing: '0.1em', color: C.faint, fontWeight: 600, marginBottom: '6px' }}>
                          EXTRACTED FROM{' '}
                          {item.import_filename
                            ? <span style={{ color: C.indigo, fontWeight: 700 }} title={item.import_filename}>{item.import_filename.length > 50 ? item.import_filename.slice(0, 47) + '...' : item.import_filename}</span>
                            : 'PDF'}
                        </div>
                        <div style={{ fontSize: '14px', fontWeight: 600, color: C.ink, marginBottom: item.original_description ? '2px' : '8px', lineHeight: 1.35 }}>
                          {item.raw_description ?? <em style={{ color: C.faint }}>No description</em>}
                        </div>
                        {item.original_description && (
                          <div style={{ fontSize: '11px', color: C.faint, marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '5px' }}>
                            <span style={{ fontSize: '9px', fontWeight: 700, background: C.monoBg, color: C.muted, padding: '1px 5px', borderRadius: '99px' }}>↳ EN</span>
                            <span title="Original text as printed on the catalogue">{item.original_description}</span>
                          </div>
                        )}
                        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                          <tbody>
                            <tr>
                              <td style={{ padding: '2px 0', color: C.faint, width: '78px' }}>supplier</td>
                              <td>
                                <select
                                  value={item.supplier_id ?? ''}
                                  onChange={e => changeItemSupplier(item, e.target.value ? Number(e.target.value) : null)}
                                  disabled={busy}
                                  title="Supplier this line belongs to — cost & supplier SKU are written here on confirm"
                                  style={{ border: '1px solid #E2E8F0', borderRadius: '4px', padding: '2px 6px', fontSize: '11px', color: item.supplier_id ? C.sub : C.amber, background: 'white', maxWidth: '190px' }}>
                                  <option value="">— pick supplier —</option>
                                  {suppliers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                                </select>
                              </td>
                            </tr>
                            <tr><td style={{ padding: '2px 0', color: C.faint, width: '78px' }}>brand</td><td style={{ color: C.muted }}>{item.brand ?? '—'}</td></tr>
                            {item.variant && <tr><td style={{ padding: '2px 0', color: C.faint, width: '78px' }}>variant</td><td><span style={{ fontSize: '10.5px', fontWeight: 700, background: '#EDE9FE', color: '#5B21B6', padding: '1px 7px', borderRadius: '99px' }}>{item.variant}</span></td></tr>}
                            {item.supplier_sku && (<tr><td style={{ padding: '2px 0', color: C.faint }}>SKU</td><td style={{ color: C.muted, fontFamily: 'monospace' }}>{item.supplier_sku}</td></tr>)}
                            <tr style={diff && !diff.fields.pack.ok ? { background: C.redBg } : {}}>
                              <td style={{ padding: diff && !diff.fields.pack.ok ? '3px 6px' : '2px 0', color: diff && !diff.fields.pack.ok ? C.bad : C.faint, fontWeight: diff && !diff.fields.pack.ok ? 600 : 400 }}>pack</td>
                              <td style={{ padding: diff && !diff.fields.pack.ok ? '3px 6px' : '2px 0', color: diff && !diff.fields.pack.ok ? C.bad : C.muted, fontWeight: diff && !diff.fields.pack.ok ? 700 : 400 }}>
                                {item.units_per_pack != null ? `${item.units_per_pack} × ${item.uom ?? 'unit'}` : (item.pack_size ?? '—')}
                              </td>
                            </tr>
                            <tr><td style={{ padding: '2px 0', color: C.faint }} title="Smallest quantity you sell at a time">min sell</td><td style={{ color: C.muted, fontVariantNumeric: 'tabular-nums' }}>{(item.min_sellable_qty ?? 1)} × {item.uom ?? 'unit'}</td></tr>
                            <tr style={diff && !diff.fields.cost.ok ? { background: C.redBg } : {}}>
                              <td style={{ padding: diff && !diff.fields.cost.ok ? '3px 6px' : '2px 0', color: diff && !diff.fields.cost.ok ? C.bad : C.faint, fontWeight: diff && !diff.fields.cost.ok ? 600 : 400 }}>cost</td>
                              <td style={{ padding: diff && !diff.fields.cost.ok ? '3px 6px' : '2px 0', color: diff && !diff.fields.cost.ok ? C.bad : C.muted, fontWeight: diff && !diff.fields.cost.ok ? 700 : 400, fontVariantNumeric: 'tabular-nums' }}>
                                {item.cost_price != null ? `HK$${item.cost_price.toFixed(0)}` : '—'}
                              </td>
                            </tr>
                            {item.max_bulk_buy_cost != null && (
                              <tr>
                                <td style={{ padding: '2px 0', color: C.faint }}>MBB</td>
                                <td style={{ color: '#0EA5E9', fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>
                                  HK${item.max_bulk_buy_cost.toFixed(0)}{item.max_bulk_buy_min_qty ? ` × ${item.max_bulk_buy_min_qty}` : ''}
                                </td>
                              </tr>
                            )}
                            {item.rrp != null && (<tr><td style={{ padding: '2px 0', color: C.faint }}>RRP</td><td style={{ color: C.muted, fontVariantNumeric: 'tabular-nums' }}>HK${item.rrp.toFixed(0)}</td></tr>)}
                            <tr>
                              <td style={{ padding: '2px 0', color: C.faint }}>species</td>
                              <td>
                                <span style={{ color: item.species ? C.muted : C.knobOff, textTransform: 'capitalize' }}>{item.species ?? '—'}</span>
                                <button
                                  onClick={() => detectSpecies(item)}
                                  disabled={speciesBusy.has(item.id)}
                                  title="Identify the target species with Claude + web search (researches the brand/product)"
                                  style={{ marginLeft: '8px', border: '1px solid #E2E8F0', background: 'white', color: C.indigoInk, fontSize: '10px', fontWeight: 600, borderRadius: '5px', padding: '1px 6px', cursor: speciesBusy.has(item.id) ? 'default' : 'pointer', opacity: speciesBusy.has(item.id) ? 0.7 : 1, display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                                >
                                  {speciesBusy.has(item.id) ? <><Spinner size={9} /> searching…</> : '🔍 web'}
                                </button>
                              </td>
                            </tr>
                            {item.weight_grams != null && (<tr><td style={{ padding: '2px 0', color: C.faint }}>weight</td><td style={{ color: C.muted, fontVariantNumeric: 'tabular-nums' }}>{fmtWeight(item.weight_grams, item.weight_unit)}</td></tr>)}
                            {item.min_purchase_qty != null && (<tr><td style={{ padding: '2px 0', color: C.faint }} title="Supplier minimum order quantity (in packs)">supplier MOQ</td><td style={{ color: C.muted, fontVariantNumeric: 'tabular-nums' }}>{item.min_purchase_qty}</td></tr>)}
                          </tbody>
                        </table>

                        {/* AI categorization & tags — confirmed here, applied on match / new-SKU */}
                        {(() => {
                          const matchTags = (!a.tags && top?.tags?.length) ? top.tags : null
                          const tags = a.tags ?? matchTags ?? item.ai_tags ?? []
                          const cat = effectiveCategory(a, item)
                          const catIsAi = !!item.ai_category && cat === item.ai_category
                          const addTag = () => {
                            const t = normTag(a.tagInput)
                            if (t && !tags.includes(t)) patchAction(item.id, { tags: [...tags, t], tagInput: '' })
                            else patchAction(item.id, { tagInput: '' })
                          }
                          return (
                            <div style={{ marginTop: '10px' }}>
                              <div style={{ fontSize: '10px', fontWeight: 700, color: C.faint, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '4px' }}>
                                Category
                              </div>
                              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '8px' }}>
                                <select
                                  value={cat}
                                  onChange={e => patchAction(item.id, { category: e.target.value })}
                                  style={{ border: '1px solid #E2E8F0', borderRadius: '5px', padding: '4px 8px', fontSize: '12px', background: 'white' }}
                                  title="Confirm the product category — drives the new-SKU leading digit"
                                >
                                  {categoryNames.map(c => <option key={c}>{c}</option>)}
                                </select>
                                {catIsAi
                                  ? <span title="AI-suggested category" style={{ fontSize: '10px', fontWeight: 700, color: C.indigoInk, background: C.primaryBg, borderRadius: '99px', padding: '2px 7px' }}>✨ AI</span>
                                  : <span title="Overridden by reviewer" style={{ fontSize: '10px', fontWeight: 700, color: C.amberInk, background: C.warnBg, borderRadius: '99px', padding: '2px 7px' }}>edited</span>}
                              </div>
                              <div style={{ fontSize: '10px', fontWeight: 700, color: C.faint, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '4px' }}>
                                Subcategory <span style={{ fontWeight: 400, textTransform: 'none' }}>(functional / clinical class — controlled list)</span>
                              </div>
                              {(() => {
                                const sub = effectiveSubcategory(a, item)
                                // Controlled vocabulary; keep the current value if somehow off-list.
                                const opts = Array.from(new Set([...subcatVocab, sub].filter(Boolean))) as string[]
                                return (
                                  <select
                                    value={sub}
                                    onChange={e => patchAction(item.id, { subcategory: e.target.value })}
                                    style={{ border: '1px solid #E2E8F0', borderRadius: '5px', padding: '4px 8px', fontSize: '12px', width: '100%', marginBottom: '8px', background: 'white' }}
                                  >
                                    <option value="">— none —</option>
                                    {opts.map(o => <option key={o} value={o}>{o}</option>)}
                                  </select>
                                )
                              })()}
                              <div style={{ fontSize: '10px', fontWeight: 700, color: C.faint, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '4px' }}>
                                Tags
                              </div>
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px', alignItems: 'center' }}>
                                {tags.length === 0 && <span style={{ fontSize: '11px', color: C.knobOff }}>none</span>}
                                {tags.map(t => (
                                  <span key={t} style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', fontSize: '11px', fontWeight: 600, background: C.primaryBg, color: C.indigoInk, padding: '2px 4px 2px 8px', borderRadius: '99px' }}>
                                    {t}
                                    <button onClick={() => patchAction(item.id, { tags: tags.filter(x => x !== t) })}
                                      title="Remove tag"
                                      style={{ border: 'none', background: 'none', color: '#818CF8', cursor: 'pointer', fontSize: '13px', lineHeight: 1, padding: '0 2px' }}>×</button>
                                  </span>
                                ))}
                                <input
                                  value={a.tagInput}
                                  onChange={e => patchAction(item.id, { tagInput: e.target.value })}
                                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addTag() } }}
                                  onBlur={addTag}
                                  placeholder="+ tag"
                                  style={{ border: '1px dashed #CBD5E1', borderRadius: '99px', padding: '2px 8px', fontSize: '11px', width: '72px' }}
                                />
                              </div>
                            </div>
                          )
                        })()}
                      </div>

                      {/* center divider + arrow */}
                      <div style={{
                        borderLeft: '1px dashed #CBD5E1', borderRight: '1px dashed #CBD5E1',
                        display: 'flex', alignItems: 'center', justifyContent: 'center', background: C.wash,
                      }}>
                        <div style={{ fontSize: '18px', color: top ? (diff?.match_grade === 'perfect' ? '#22C55E' : C.faint) : C.knobOff }}>⇔</div>
                      </div>

                      {/* RIGHT: IMS match OR context */}
                      <div style={{ padding: '12px 16px' }}>
                        {top ? (
                          <>
                            <div style={{ fontSize: '10px', letterSpacing: '0.1em', color: C.faint, fontWeight: 600, marginBottom: '6px', display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
                              <span>{a.pickedMatch ? 'SELECTED' : 'IMS MATCH'} · <span style={{ color: C.indigo }}>{top.sku_code}</span></span>
                              {a.pickedMatch
                                ? <span style={{ letterSpacing: 'normal', background: '#EDE9FE', color: '#5B21B6', padding: '1px 6px', borderRadius: '99px', fontSize: '9px', fontWeight: 700 }}>✋ manually selected · confirm to apply</span>
                                : <MatchPill type={top.match_type} />}
                              {top.status && top.status !== 'ACTIVE' && (
                                <span style={{ letterSpacing: 'normal', background: C.warnBg, color: C.amberInk, padding: '1px 6px', borderRadius: '99px', fontSize: '9px', fontWeight: 700 }}
                                  title="This SKU is not active — matching it will reactivate it.">
                                  {top.status}
                                </span>
                              )}
                            </div>
                            {/* SKU title — editable: typing a new title renames the product on confirm */}
                            <input
                              value={a.matchName || top.name}
                              onChange={e => patchAction(item.id, { matchName: e.target.value })}
                              title="SKU title — edit to rename this product when you confirm the match"
                              style={{ fontSize: '14px', fontWeight: 600, color: C.ink, marginBottom: '2px', lineHeight: 1.35, width: '100%', boxSizing: 'border-box', border: '1px dashed #E2E8F0', borderRadius: '5px', padding: '3px 7px', background: a.matchName && a.matchName !== top.name ? '#FFFBEB' : 'white' }}
                            />
                            <p style={{ fontSize: '10px', color: a.matchName && a.matchName !== top.name ? C.amberInk : C.knobOff, margin: '0 0 8px' }}>
                              {a.matchName && a.matchName !== top.name ? '✎ SKU title will be renamed on confirm' : 'editable — type to rename the SKU title on confirm'}
                            </p>
                            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                              <tbody>
                                <tr><td style={{ padding: '2px 0', color: C.faint, width: '78px' }}>brand</td><td style={{ color: C.muted }}>{top.brand ?? '—'}</td></tr>
                                <tr style={diff && !diff.fields.pack.ok ? { background: C.redBg } : {}}>
                                  <td style={{ padding: diff && !diff.fields.pack.ok ? '3px 6px' : '2px 0', color: diff && !diff.fields.pack.ok ? C.bad : C.faint, fontWeight: diff && !diff.fields.pack.ok ? 600 : 400 }}>pack</td>
                                  <td style={{ padding: diff && !diff.fields.pack.ok ? '3px 6px' : '2px 0', color: diff && !diff.fields.pack.ok ? C.bad : C.muted, fontWeight: diff && !diff.fields.pack.ok ? 700 : 400 }}>
                                    {top.units_per_pack != null ? `${top.units_per_pack} × ${top.uom ?? 'unit'}` : '—'}
                                  </td>
                                </tr>
                                <tr style={diff && !diff.fields.cost.ok ? { background: C.redBg } : {}}>
                                  <td style={{ padding: diff && !diff.fields.cost.ok ? '3px 6px' : '2px 0', color: diff && !diff.fields.cost.ok ? C.bad : C.faint, fontWeight: diff && !diff.fields.cost.ok ? 600 : 400 }}>cost</td>
                                  <td style={{ padding: diff && !diff.fields.cost.ok ? '3px 6px' : '2px 0', color: diff && !diff.fields.cost.ok ? C.bad : C.muted, fontWeight: diff && !diff.fields.cost.ok ? 700 : 400, fontVariantNumeric: 'tabular-nums' }}>
                                    {top.basic_cost != null ? `HK$${top.basic_cost.toFixed(0)}` : '— no IMS cost'}
                                  </td>
                                </tr>
                              </tbody>
                            </table>
                            {item.suggested_matches.length > 1 && (
                              <p style={{ fontSize: '10px', color: C.faint, marginTop: '8px', fontStyle: 'italic' }}>
                                +{item.suggested_matches.length - 1} other suggestion{item.suggested_matches.length > 2 ? 's' : ''} available
                              </p>
                            )}
                          </>
                        ) : (
                          // No match — context panel
                          <>
                            <div style={{ fontSize: '10px', letterSpacing: '0.1em', color: C.faint, fontWeight: 600, marginBottom: '6px' }}>NO IMS MATCH — CONTEXT</div>
                            {item.tier === 't3' ? (
                              <>
                                <div style={{ fontSize: '13px', fontWeight: 600, color: C.redInk, marginBottom: '8px' }}>
                                  Brand <strong>{item.brand}</strong> has 0 SKUs in your IMS
                                </div>
                                <p style={{ fontSize: '11.5px', color: C.sub, lineHeight: 1.55 }}>
                                  You likely don&apos;t carry this brand. Recommended: <strong style={{ color: C.redInk }}>✗ Reject</strong>.
                                  Use + New SKU only if you want to start carrying it.
                                </p>
                              </>
                            ) : item.tier === 't4' ? (
                              <>
                                <div style={{ fontSize: '13px', fontWeight: 600, color: '#6B21A8', marginBottom: '8px' }}>
                                  Brand <strong>{item.brand}</strong> is in your IMS, but no exact match
                                </div>
                                <p style={{ fontSize: '11.5px', color: C.sub, lineHeight: 1.55 }}>
                                  Could be a variant the matcher missed, or a SKU you don&apos;t stock yet.
                                  Recommended: <strong style={{ color: '#6B21A8' }}>🔍 Find &amp; Match</strong> first; if no luck, <strong style={{ color: C.green }}>+ New SKU</strong>.
                                </p>
                              </>
                            ) : (
                              <p style={{ fontSize: '12px', color: C.faint, fontStyle: 'italic' }}>
                                No suggested match. Use Find &amp; Match or + New SKU.
                              </p>
                            )}
                          </>
                        )}
                      </div>
                    </div>

                    {/* Actions row */}
                    <div style={{ display: 'flex', gap: '8px', padding: '10px 14px', background: C.wash, borderTop: '1px solid #E2E8F0', alignItems: 'center', flexWrap: 'wrap' }}>
                      {a.mode === 'idle' && (
                        <>
                          {top && (
                            <button
                              onClick={() => doMatch(item, top.sku_code, top.name)}
                              disabled={busy}
                              aria-busy={busyKind(item.id) === 'match' || undefined}
                              style={{
                                padding: '6px 14px', fontSize: '12px', fontWeight: 600,
                                background: busy ? C.monoBg : (diff?.match_grade === 'perfect' ? '#22C55E' : (diff?.match_grade === 'partial' ? '#F59E0B' : C.panel)),
                                color: busy ? C.muted : (diff?.match_grade === 'weak' ? C.sub : 'white'),
                                border: (busy || diff?.match_grade === 'weak') ? '1px solid #CBD5E1' : 'none',
                                borderRadius: '5px', cursor: busy ? 'default' : 'pointer',
                                display: 'inline-flex', alignItems: 'center', gap: '6px',
                              }}
                            >
                              {busyKind(item.id) === 'match'
                                ? <><Spinner color={C.muted} /> Confirming…</>
                                : (a.pickedMatch ? '✓ Confirm selected SKU' : '✓ Confirm match')}
                            </button>
                          )}
                          {a.pickedMatch && (
                            <button onClick={() => patchAction(item.id, { pickedMatch: null })} disabled={busy}
                              title="Discard the SKU you picked and go back to the suggested match"
                              style={{ padding: '6px 12px', fontSize: '12px', background: 'white', color: C.amberInk, border: '1px solid #FDE68A', borderRadius: '5px', cursor: 'pointer' }}>
                              ↩ Undo pick
                            </button>
                          )}
                          <button onClick={() => { patchAction(item.id, { mode: 'match_manual', manualSku: '' }); setSkuResults(p => ({ ...p, [item.id]: [] })) }} disabled={busy} style={{ padding: '6px 14px', fontSize: '12px', background: 'white', color: C.sub, border: '1px solid #CBD5E1', borderRadius: '5px', cursor: 'pointer' }}
                            title={top ? 'Replace the suggested match with a different inventory item' : 'Search inventory and match to a SKU'}>
                            {top ? '🔁 Match a different SKU' : '🔍 Find & Match'}
                          </button>
                          <button onClick={() => patchAction(item.id, { mode: 'new_sku' })} disabled={busy} style={{ padding: '6px 14px', fontSize: '12px', background: 'white', color: C.green, border: '1px solid #BBF7D0', borderRadius: '5px', cursor: 'pointer' }}>
                            + New SKU
                          </button>
                          <button onClick={() => patchAction(item.id, { mode: 'reject' })} disabled={busy} style={{ padding: '6px 14px', fontSize: '12px', background: 'white', color: C.redInk, border: '1px solid #FECACA', borderRadius: '5px', cursor: 'pointer' }}>
                            ✗ Reject
                          </button>
                          <button onClick={() => patchAction(item.id, { mode: 'edit', edit: seedEdit(item) })} disabled={busy} style={{ padding: '6px 14px', fontSize: '12px', background: 'white', color: C.sub, border: '1px solid #E2E8F0', borderRadius: '5px', cursor: 'pointer' }}>
                            ✎ Edit
                          </button>
                          {item.skipped
                            ? <button onClick={() => doUnskip(item)} disabled={busy} title="Return this item to the active review queue" style={{ padding: '6px 14px', fontSize: '12px', background: 'white', color: '#0369A1', border: '1px solid #BAE6FD', borderRadius: '5px', cursor: 'pointer' }}>↩ Un-skip</button>
                            : <button onClick={() => doSkip(item)} disabled={busy} title="Set aside for later — moves to the Skipped bucket" style={{ padding: '6px 14px', fontSize: '12px', background: 'white', color: C.amberInk, border: '1px solid #FDE68A', borderRadius: '5px', cursor: 'pointer' }}>⏭ Skip</button>}
                        </>
                      )}
                          {a.mode === 'match_manual' && (
                            <>
                              <input
                                type="text"
                                placeholder="Search inventory — name, brand, or SKU…"
                                value={a.manualSku}
                                onChange={e => { patchAction(item.id, { manualSku: e.target.value }); runSkuSearch(item.id, e.target.value) }}
                                onKeyDown={e => { if (e.key === 'Enter' && skuResults[item.id]?.[0]) pickMatch(item, skuResults[item.id][0]) }}
                                style={{ border: '1px solid #E2E8F0', borderRadius: '5px', padding: '5px 10px', fontSize: '12px', width: '300px' }}
                                autoFocus
                              />
                              {skuSearching[item.id] && <Spinner color={C.faint} />}
                              <Ghost onClick={() => patchAction(item.id, { mode: 'idle' })}>Back</Ghost>
                              {/* Search results — click a row to (re)match this scan to that inventory SKU. */}
                              {(skuResults[item.id]?.length ?? 0) > 0 && (
                                <div style={{ flexBasis: '100%', marginTop: '6px', border: '1px solid #E2E8F0', borderRadius: '6px', maxHeight: '220px', overflowY: 'auto', background: 'white' }}>
                                  {skuResults[item.id].map(r => (
                                    <button key={r.sku_code} type="button" onClick={() => pickMatch(item, r)} disabled={busy}
                                      title="Select this SKU as the match — you'll confirm it next"
                                      style={{ display: 'flex', width: '100%', textAlign: 'left', gap: '8px', alignItems: 'center', padding: '6px 10px', border: 'none', borderBottom: '1px solid #F1F5F9', background: top?.sku_code === r.sku_code ? C.primaryBg : 'white', cursor: busy ? 'default' : 'pointer', fontSize: '12px' }}>
                                      <code style={{ color: C.indigoInk, fontFamily: 'monospace' }}>{r.sku_code}</code>
                                      <span style={{ fontWeight: 600, color: C.ink, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.name}</span>
                                      {r.brand && <span style={{ color: C.muted, whiteSpace: 'nowrap' }}>{r.brand}</span>}
                                      {(r.basic_cost ?? r.primary_cost) != null && <span style={{ color: C.muted, fontVariantNumeric: 'tabular-nums' }}>HK${((r.basic_cost ?? r.primary_cost) as number).toFixed(0)}</span>}
                                      {r.status && r.status !== 'ACTIVE' && <span style={{ fontSize: '9px', fontWeight: 700, color: C.amberInk, background: C.warnBg, padding: '1px 5px', borderRadius: '99px' }}>{r.status}</span>}
                                    </button>
                                  ))}
                                </div>
                              )}
                              {a.manualSku.trim().length >= 2 && !skuSearching[item.id] && (skuResults[item.id]?.length ?? 0) === 0 && (
                                <span style={{ flexBasis: '100%', fontSize: '11px', color: C.faint, marginTop: '4px' }}>
                                  No inventory items match “{a.manualSku.trim()}”. Refine your search to pick a SKU.
                                </span>
                              )}
                            </>
                          )}
                          {a.mode === 'new_sku' && (
                            <>
                              <span style={{ fontSize: '11px', color: C.muted, whiteSpace: 'nowrap' }}
                                title="Confirm the category in the panel above">
                                {effectiveCategory(a, item)}
                              </span>
                              <span style={{ fontSize: '12px', fontFamily: 'ui-monospace, monospace', color: C.ink, background: C.monoBg, border: '1px solid #E2E8F0', borderRadius: '5px', padding: '4px 8px', whiteSpace: 'nowrap' }}
                                title="Internal SKU that will be generated on confirm">
                                → {(categoryDigit[effectiveCategory(a, item)] ?? '?')}{nextSuffix ?? '·······'}
                              </span>
                              <input
                                type="text"
                                placeholder={item.raw_description ?? 'Product name'}
                                value={a.name}
                                onChange={e => patchAction(item.id, { name: e.target.value })}
                                style={{ border: '1px solid #E2E8F0', borderRadius: '5px', padding: '5px 10px', fontSize: '12px', width: '240px' }}
                              />
                              <input
                                type="text"
                                list="brand-list"
                                placeholder={item.brand ?? 'Brand'}
                                value={a.brand}
                                onChange={e => patchAction(item.id, { brand: e.target.value })}
                                style={{ border: '1px solid #E2E8F0', borderRadius: '5px', padding: '5px 10px', fontSize: '12px', width: '120px' }}
                              />
                              {(() => {
                                const b = a.brand.trim()
                                if (!b) return null
                                const known = brandSet.has(normTag(b))
                                return known
                                  ? <span title="Brand is in the list" style={{ fontSize: '11px', fontWeight: 700, color: '#16A34A' }}>✓ in list</span>
                                  : <button onClick={() => addBrand(b, item.supplier_id)} title="Add this brand to the list"
                                      style={{ fontSize: '11px', fontWeight: 700, color: C.amberInk, background: C.warnBg, border: '1px solid #FDE68A', borderRadius: '5px', padding: '4px 8px', cursor: 'pointer', whiteSpace: 'nowrap' }}>+ add brand</button>
                              })()}
                              <Btn onClick={() => doAssignNew(item)} disabled={busy}
                                   loading={busyKind(item.id) === 'assign'} loadingLabel="Creating SKU…" bg="#22C55E" color="white">
                                Create SKU
                              </Btn>
                              <Ghost onClick={() => patchAction(item.id, { mode: 'idle' })}>Back</Ghost>
                            </>
                          )}
                          {a.mode === 'reject' && (
                            <>
                              <select
                                value={a.rejectReason}
                                onChange={e => patchAction(item.id, { rejectReason: e.target.value })}
                                style={{ border: '1px solid #E2E8F0', borderRadius: '5px', padding: '5px 8px', fontSize: '12px', background: 'white' }}
                              >
                                <option value="clinical_consumable">Clinical consumable</option>
                                <option value="duplicate">Duplicate</option>
                                <option value="out_of_scope">Out of scope — we don&apos;t carry this</option>
                                <option value="discontinued">Discontinued</option>
                              </select>
                              <Btn onClick={() => doReject(item)} disabled={busy}
                                   loading={busyKind(item.id) === 'reject'} loadingLabel="Rejecting…" bg={C.redBg} color={C.redInk}>
                                Confirm reject
                              </Btn>
                              <Ghost onClick={() => patchAction(item.id, { mode: 'idle' })}>Back</Ghost>
                            </>
                          )}
                          {a.mode === 'edit' && a.edit && (
                            <div style={{ flexBasis: '100%', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                              <p style={{ fontSize: '11px', color: C.muted, margin: 0 }}>
                                Correct any mis-scanned field, then save — match suggestions re-rank automatically.
                              </p>
                              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '8px' }}>
                                <EditField label="Description"   value={a.edit.raw_description} onChange={v => patchEdit(item.id, { raw_description: v })} wide />
                                <EditField label="Brand"         value={a.edit.brand}          onChange={v => patchEdit(item.id, { brand: v })} list="brand-list" />
                                <EditField label="Variant (size)" value={a.edit.variant}       onChange={v => patchEdit(item.id, { variant: v })} />
                                <EditField label="Supplier SKU"  value={a.edit.supplier_sku}   onChange={v => patchEdit(item.id, { supplier_sku: v })} />
                                <EditField label="Barcode"       value={a.edit.barcode}        onChange={v => patchEdit(item.id, { barcode: v })} />
                                <EditField label="Cost (HK$)"    value={a.edit.cost_price}     onChange={v => patchEdit(item.id, { cost_price: v })} type="number" />
                                <EditField label="Units / pack"  value={a.edit.units_per_pack} onChange={v => patchEdit(item.id, { units_per_pack: v })} type="number" />
                                <EditField label="Min sellable"  value={a.edit.min_sellable_qty} onChange={v => patchEdit(item.id, { min_sellable_qty: v })} type="number" />
                                <EditField label="Sell UOM"      value={a.edit.uom}            onChange={v => patchEdit(item.id, { uom: v })} options={UOM_OPTIONS} />
                                <EditField label="Species"       value={a.edit.species}        onChange={v => patchEdit(item.id, { species: v })} options={['dog', 'cat', 'both', 'other']} />
                                <EditField label="Weight"        value={a.edit.weight_value}   onChange={v => patchEdit(item.id, { weight_value: v })} type="number" />
                                <EditField label="Weight unit"   value={a.edit.weight_unit}    onChange={v => patchEdit(item.id, { weight_unit: v })} options={['kg', 'lb']} />
                                <EditField label="RRP (HK$)"     value={a.edit.rrp}            onChange={v => patchEdit(item.id, { rrp: v })} type="number" />
                                <EditField label="Supplier MOQ"  value={a.edit.min_purchase_qty} onChange={v => patchEdit(item.id, { min_purchase_qty: v })} type="number" />
                                <EditField label="Pack size (printed)" value={a.edit.pack_size} onChange={v => patchEdit(item.id, { pack_size: v })} />
                                <EditField label="MBB cost (HK$)" value={a.edit.max_bulk_buy_cost} onChange={v => patchEdit(item.id, { max_bulk_buy_cost: v })} type="number" />
                                <EditField label="MBB min qty"   value={a.edit.max_bulk_buy_min_qty} onChange={v => patchEdit(item.id, { max_bulk_buy_min_qty: v })} type="number" />
                                <label style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                                  <span style={{ fontSize: '10px', fontWeight: 600, color: C.faint }}>Supplier</span>
                                  <select value={a.edit.supplier_id} onChange={e => patchEdit(item.id, { supplier_id: e.target.value })}
                                    style={{ border: '1px solid #E2E8F0', borderRadius: '5px', padding: '5px 10px', fontSize: '12px', width: '100%', boxSizing: 'border-box', background: 'white' }}>
                                    <option value="">—</option>
                                    {suppliers.map(s => <option key={s.id} value={String(s.id)}>{s.name}</option>)}
                                  </select>
                                </label>
                                <EditField label="Bulk-buy tiers" value={a.edit.bulk_buy_tiers} onChange={v => patchEdit(item.id, { bulk_buy_tiers: v })} wide />
                              </div>
                              <div style={{ display: 'flex', gap: '8px' }}>
                                <Btn onClick={() => doEdit(item)} disabled={busy}
                                     loading={busyKind(item.id) === 'edit'} loadingLabel="Saving…" bg={C.indigo} color="white">Save changes</Btn>
                                <Ghost onClick={() => patchAction(item.id, { mode: 'idle', edit: null })}>Cancel</Ghost>
                              </div>
                            </div>
                          )}
                    </div>
                  </div>
                )
              })}
              {visibleItems.length > shownItems.length && (
                <div style={{ textAlign: 'center', padding: '14px' }}>
                  <button onClick={() => setRenderCount(c => c + 50)}
                    style={{ background: 'white', border: '1px solid #E2E8F0', borderRadius: '7px', padding: '8px 18px', fontSize: '12px', fontWeight: 600, color: C.indigo, cursor: 'pointer' }}>
                    Show 50 more ({visibleItems.length - shownItems.length} not rendered)
                  </button>
                </div>
              )}
            </>
          )}

          </>)}
          {/* ════════ end REVIEW view ════════ */}

          {/* ════════ SKIPPED view — compact set-aside list ════════ */}
          {view === 'skipped' && (<>
            <OnboardFilterBar search={itemSearch} setSearch={setItemSearch}
              supplier={supplierFilter} setSupplier={setSupplierFilter} supplierFacets={supplierFacets}
              user={skippedByFilter} setUser={setSkippedByFilter} userFacets={userFacets}
              userLabel="All who skipped" suppliers={suppliers} />
            <div style={{ background: 'white', border: '1px solid #E8EDF3', borderRadius: '12px', boxShadow: '0 1px 2px rgba(15,23,42,0.04)', overflow: 'hidden' }}>
              {queue.length === 0 ? (
                <div style={{ padding: '36px', textAlign: 'center', color: C.faint, fontSize: '13px' }}>
                  Nothing skipped. Items you set aside for later land here — un-skip to return them to the review queue.
                </div>
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12.5px' }}>
                  <thead>
                    <tr style={{ background: C.wash, textAlign: 'left' }}>
                      <th style={thCell}>Item</th>
                      <th style={thCell}>Supplier</th>
                      <th style={{ ...thCell, textAlign: 'right' }}>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {queue.map((it, idx) => {
                      const busy = processing.has(it.id)
                      const sup = suppliers.find(s => s.id === it.supplier_id)?.name
                      return (
                        <tr key={it.id} style={{ borderTop: idx ? '1px solid #F1F5F9' : 'none' }}>
                          <td style={tdCell}>
                            <div style={{ color: C.ink, fontWeight: 500 }}>{it.raw_description || '—'}</div>
                            {it.brand && <div style={{ color: C.faint, fontSize: '11px' }}>{it.brand}</div>}
                          </td>
                          <td style={{ ...tdCell, color: C.muted, whiteSpace: 'nowrap' }}>{sup || '—'}</td>
                          <td style={{ ...tdCell, textAlign: 'right', whiteSpace: 'nowrap' }}>
                            <button onClick={() => doUnskip(it)} disabled={busy}
                              style={{ padding: '5px 12px', fontSize: '12px', fontWeight: 600, background: 'white', color: C.amberInk, border: '1px solid #FDE68A', borderRadius: '6px', cursor: busy ? 'default' : 'pointer' }}>
                              {busy ? '…' : '↩ Un-skip'}
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </>)}

          {/* ════════ CONFIRMED view — compact list of matched / new-SKU items ════════ */}
          {view === 'confirmed' && (<>
            <OnboardFilterBar search={confSearchInput} setSearch={setConfSearchInput}
              supplier={confSupplier} setSupplier={setConfSupplier} supplierFacets={confSupplierFacets}
              user={confUser} setUser={setConfUser} userFacets={confUserFacets}
              userLabel="All reviewers" suppliers={suppliers} />
            <div style={{ background: 'white', border: '1px solid #E8EDF3', borderRadius: '12px', boxShadow: '0 1px 2px rgba(15,23,42,0.04)', overflow: 'hidden' }}>
              {confirmedLoading && confirmed.length === 0 ? (
                <div style={{ padding: '36px', textAlign: 'center', color: C.faint, fontSize: '13px' }}>Loading…</div>
              ) : confirmed.length === 0 ? (
                <div style={{ padding: '36px', textAlign: 'center', color: C.faint, fontSize: '13px' }}>
                  No confirmed items yet. Items you match to a SKU or assign a new one appear here.
                </div>
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12.5px' }}>
                  <thead>
                    <tr style={{ background: C.wash, textAlign: 'left' }}>
                      <th style={thCell}>Item</th>
                      <th style={thCell}>Outcome</th>
                      <th style={thCell}>SKU</th>
                      <th style={thCell}>Reviewed</th>
                      <th style={{ ...thCell, textAlign: 'right' }}>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {confirmed.map((it, idx) => {
                      const busy = processing.has(it.id)
                      const isNew = it.action === 'new_sku'
                      const skuHref = it.sku ? `/items/${skuToPath(it.sku)}` : null
                      return (
                        <tr key={it.id} style={{ borderTop: idx ? '1px solid #F1F5F9' : 'none' }}>
                          <td style={tdCell}>
                            <div style={{ color: C.ink, fontWeight: 500 }}>{it.product_name || it.raw_description || '—'}</div>
                            {it.product_name && it.raw_description && it.raw_description !== it.product_name &&
                              <div style={{ color: C.faint, fontSize: '11px' }}>{it.raw_description}</div>}
                            {it.supplier_name && <div style={{ color: C.knobOff, fontSize: '11px' }}>{it.supplier_name}</div>}
                          </td>
                          <td style={tdCell}>
                            <span style={{ display: 'inline-block', padding: '2px 8px', borderRadius: '999px', fontSize: '11px', fontWeight: 700, background: isNew ? C.greenBg : '#DBEAFE', color: isNew ? C.green : '#1E40AF' }}>
                              {isNew ? 'New SKU' : 'Matched'}
                            </span>
                          </td>
                          <td style={tdCell}>
                            {skuHref
                              ? <a href={skuHref} target="_blank" rel="noopener noreferrer"
                                  style={{ color: C.indigo, fontWeight: 600, textDecoration: 'none', fontVariantNumeric: 'tabular-nums' }}>{it.sku} ↗</a>
                              : <span style={{ color: C.knobOff }}>—</span>}
                          </td>
                          <td style={{ ...tdCell, color: C.muted, whiteSpace: 'nowrap' }}>
                            {it.reviewed_by || '—'}
                            {it.reviewed_at && <div style={{ color: C.faint, fontSize: '11px' }}>{fmtWhen(it.reviewed_at)}</div>}
                          </td>
                          <td style={{ ...tdCell, textAlign: 'right', whiteSpace: 'nowrap' }}>
                            {skuHref && (
                              <a href={skuHref} target="_blank" rel="noopener noreferrer"
                                style={{ padding: '5px 10px', fontSize: '12px', fontWeight: 600, background: 'white', color: C.indigoInk, border: '1px solid #E0E7FF', borderRadius: '6px', textDecoration: 'none', marginRight: '6px' }}>View</a>
                            )}
                            {can('catalogue_onboard') && (
                              <button onClick={() => doUnconfirm(it)} disabled={busy}
                                style={{ padding: '5px 10px', fontSize: '12px', fontWeight: 600, background: 'white', color: C.bad, border: '1px solid #FECACA', borderRadius: '6px', cursor: busy ? 'default' : 'pointer' }}>
                                {busy ? '…' : '↩ Unconfirm'}
                              </button>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </>)}

        </div>

        {/* ══ History & activity — imports · scans · onboarding actions (collapsed) ══ */}
        {imports.length > 0 && (
          <div style={{ background: 'white', border: '1px solid #E8EDF3', borderRadius: '12px', boxShadow: '0 1px 2px rgba(15,23,42,0.04)', overflow: 'hidden', marginTop: '8px' }}>
            <button onClick={() => setShowHistory(h => !h)}
              style={{ width: '100%', border: 'none', background: C.wash, cursor: 'pointer', padding: '14px 18px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: showHistory ? '1px solid #E2E8F0' : 'none' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                <span style={{ fontSize: '14px', fontWeight: 700, color: C.ink }}>History &amp; activity</span>
                <span style={{ fontSize: '12px', color: C.faint }}>
                  {imports.length} imports{scanLog ? ` · ${scanLog.total_items.toLocaleString()} items scanned` : ''}{scanLog?.failed ? ` · ${scanLog.failed} failed` : ''}{daily && daily.totals.processed > 0 ? ` · ${daily.totals.processed.toLocaleString()} processed (30d)` : ''}
                </span>
                {batchRunning && <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '11px', fontWeight: 700, color: C.ok }}><span className="ims-live-dot" /> live</span>}
              </div>
              <span style={{ fontSize: '12px', color: C.faint }}>{showHistory ? 'Hide' : 'Show'}</span>
            </button>
            {showHistory && (
              <div>
                <div style={{ display: 'flex', gap: '4px', padding: '10px 14px', borderBottom: '1px solid #F1F5F9' }}>
                  {([['daily', '📅 Daily report'], ['imports', `Imports (${imports.length})`], ['scans', `Scan log${scanLog ? ` (${scanLog.successful})` : ''}`], ['activity', `Activity (${audit.length})`]] as const).map(([k, lbl]) => (
                    <button key={k} onClick={() => setHistTab(k)} style={{ padding: '5px 12px', fontSize: '12px', fontWeight: 600, border: 'none', borderRadius: '6px', cursor: 'pointer', background: histTab === k ? C.primaryBg : 'transparent', color: histTab === k ? C.indigoInk : C.muted }}>{lbl}</button>
                  ))}
                </div>

                {/* Daily report tab — per-day onboarding throughput */}
                {histTab === 'daily' && (
                  daily && daily.days.length > 0 ? (() => {
                    const maxTotal = Math.max(1, ...daily.days.map(r => r.total))
                    return (
                      <div>
                        {/* 30-day summary strip */}
                        <div style={{ display: 'flex', gap: '22px', flexWrap: 'wrap', alignItems: 'flex-end', padding: '14px 18px', borderBottom: '1px solid #F1F5F9', background: '#FBFCFE' }}>
                          {([
                            ['Processed', daily.totals.processed, C.ink],
                            ['Matched', daily.totals.matched, '#1E40AF'],
                            ['New SKU', daily.totals.new_sku, C.green],
                            ['Rejected', daily.totals.rejected, C.muted],
                            ['Skipped', daily.totals.skipped, C.amber],
                            ['Active days', daily.totals.active_days, C.indigoInk],
                          ] as const).map(([lbl, val, color]) => (
                            <div key={lbl}>
                              <div style={{ fontSize: '20px', fontWeight: 700, color, fontVariantNumeric: 'tabular-nums', lineHeight: 1 }}>{val.toLocaleString()}</div>
                              <div style={{ fontSize: '10px', fontWeight: 600, color: C.faint, textTransform: 'uppercase', letterSpacing: '0.05em', marginTop: '3px' }}>{lbl}</div>
                            </div>
                          ))}
                          <span style={{ marginLeft: 'auto', fontSize: '11px', color: C.faint }}>
                            since {daily.from}{dailyLoading ? ' · refreshing…' : ''}
                          </span>
                        </div>
                        {/* Per-day rows with stacked activity bars */}
                        <div style={{ maxHeight: '460px', overflowY: 'auto' }}>
                          {daily.days.map((r, idx) => {
                            const segs = [
                              { v: r.matched,  c: '#3B82F6', label: 'matched' },
                              { v: r.new_sku,  c: '#22C55E', label: 'new SKU' },
                              { v: r.rejected, c: C.faint, label: 'rejected' },
                              { v: r.skipped,  c: '#F59E0B', label: 'skipped' },
                            ].filter(s => s.v > 0)
                            const dt = new Date(r.date + 'T00:00:00')
                            return (
                              <div key={r.date} style={{ padding: '10px 18px', borderBottom: idx < daily.days.length - 1 ? '1px solid #F1F5F9' : 'none' }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                                  <div style={{ width: '92px', flexShrink: 0 }}>
                                    <span style={{ fontSize: '12.5px', fontWeight: 600, color: C.ink }}>{dt.toLocaleDateString('en-HK', { day: 'numeric', month: 'short' })}</span>
                                    <span style={{ fontSize: '10px', color: C.faint, marginLeft: '6px' }}>{dt.toLocaleDateString('en-HK', { weekday: 'short' })}</span>
                                  </div>
                                  <div title={`${r.total} total`} style={{ flex: 1, minWidth: 0, height: '18px', display: 'flex', borderRadius: '4px', overflow: 'hidden', background: C.monoBg }}>
                                    {segs.map((s, i) => <div key={i} title={`${s.v} ${s.label}`} style={{ width: `${(s.v / maxTotal) * 100}%`, background: s.c }} />)}
                                  </div>
                                  <span style={{ width: '44px', textAlign: 'right', flexShrink: 0, fontSize: '14px', fontWeight: 700, color: C.ink, fontVariantNumeric: 'tabular-nums' }}>{r.processed}</span>
                                </div>
                                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', marginTop: '5px', marginLeft: '104px', fontSize: '11px', fontVariantNumeric: 'tabular-nums', alignItems: 'baseline' }}>
                                  {r.matched  > 0 && <span style={{ color: '#1E40AF' }}>● {r.matched} matched</span>}
                                  {r.new_sku  > 0 && <span style={{ color: C.green }}>● {r.new_sku} new SKU</span>}
                                  {r.rejected > 0 && <span style={{ color: C.muted }}>● {r.rejected} rejected</span>}
                                  {r.skipped  > 0 && <span style={{ color: C.amber }}>● {r.skipped} skipped</span>}
                                  {r.reviewers.length > 0 && <span style={{ color: C.faint, marginLeft: '2px' }}>· {r.reviewers.join(', ')}</span>}
                                </div>
                              </div>
                            )
                          })}
                        </div>
                      </div>
                    )
                  })() : <p style={{ fontSize: '12px', color: C.faint, padding: '16px 18px', margin: 0 }}>{daily ? 'No onboarding activity in the last 30 days.' : 'Loading daily report…'}</p>
                )}

                {/* Imports tab */}
                {histTab === 'imports' && (
                  <div style={{ maxHeight: '420px', overflowY: 'auto' }}>
                    {imports.map((imp, idx) => (
                      <div key={imp.id} style={{ padding: '12px 16px', borderBottom: idx < imports.length - 1 ? '1px solid #F1F5F9' : 'none', display: 'flex', alignItems: 'center', gap: '14px' }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: '13px', fontWeight: 500, color: C.ink, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{imp.filename}</div>
                          <div style={{ fontSize: '11px', color: C.faint, marginTop: '2px' }}>
                            {imp.supplier_name ?? 'Unknown supplier'} · {imp.format.toUpperCase()} · {new Date(imp.imported_at + 'Z').toLocaleDateString('en-HK', { day: 'numeric', month: 'short', year: 'numeric' })}
                          </div>
                        </div>
                        <div style={{ display: 'flex', gap: '6px', flexShrink: 0, alignItems: 'center' }}>
                          {imp.counts.pending  > 0 && <Pill bg={C.warnBg} color={C.amberInk}>{imp.counts.pending} pending</Pill>}
                          {imp.counts.matched  > 0 && <Pill bg="#DBEAFE" color="#1E40AF">{imp.counts.matched} matched</Pill>}
                          {imp.counts.new_sku  > 0 && <Pill bg={C.greenBg} color={C.green}>{imp.counts.new_sku} new SKU</Pill>}
                          {imp.counts.rejected > 0 && <Pill bg={C.monoBg} color={C.muted}>{imp.counts.rejected} rejected</Pill>}
                          {imp.item_count === 0 && <span style={{ fontSize: '11px', color: C.knobOff }}>0 items</span>}
                          {imp.counts.pending > 0 && (
                            <button onClick={() => doTranslateImport(imp.id)} disabled={translatingImport === imp.id}
                              title="Translate this scan's pending items to English (already-English items are left as-is)"
                              style={{ padding: '4px 10px', fontSize: '11px', fontWeight: 600, background: 'white', color: C.indigoInk, border: '1px solid #C7D2FE', borderRadius: '5px', cursor: translatingImport === imp.id ? 'default' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: '5px' }}>
                              {translatingImport === imp.id ? <><Spinner size={10} /> Translating…</> : '🌐 Translate'}
                            </button>
                          )}
                          {imp.item_count > 0 && can('catalogue_onboard') && (
                            <ReparseButton scope="import" refId={imp.id} label="↻ Re-parse"
                              title={`Re-parse every line from ${imp.filename} and review the diff`}
                              style={{ padding: '4px 10px', fontSize: '11px', fontWeight: 600, background: 'white', color: C.indigoInk, border: '1px solid #C7D2FE', borderRadius: '5px' }} />
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {/* Scan log tab */}
                {histTab === 'scans' && (scanLog ? (
                  <div style={{ maxHeight: '420px', overflowY: 'auto' }}>
                    <div style={{ display: 'grid', gridTemplateColumns: '32px 200px 1fr 80px 80px 80px', gap: '10px', padding: '8px 18px', background: '#FAFAFA', fontSize: '10px', fontWeight: 700, color: C.faint, textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: '1px solid #E2E8F0', position: 'sticky', top: 0 }}>
                      <span>#</span><span>Supplier</span><span>Source file</span><span style={{ textAlign: 'right' }}>Items</span><span style={{ textAlign: 'right' }}>Errors</span><span style={{ textAlign: 'center' }}>Status</span>
                    </div>
                    {scanLog.log.map((entry, i) => (
                      <div key={entry.import_id} style={{ display: 'grid', gridTemplateColumns: '32px 200px 1fr 80px 80px 80px', gap: '10px', padding: '8px 18px', fontSize: '12px', borderBottom: i < scanLog.log.length - 1 ? '1px solid #F1F5F9' : 'none', background: entry.status === 'ok' ? 'white' : (entry.status === 'error' ? C.badBg : '#FAFAFA') }}>
                        <span style={{ color: C.faint, fontVariantNumeric: 'tabular-nums' }}>{entry.import_id}</span>
                        <span style={{ color: C.ink, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{entry.supplier_name ?? <em style={{ color: C.faint }}>unlinked</em>}</span>
                        <span style={{ color: C.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: '11px' }} title={entry.filename}>{entry.filename}</span>
                        <span style={{ textAlign: 'right', fontWeight: 600, color: entry.real_items > 0 ? C.green : C.knobOff, fontVariantNumeric: 'tabular-nums' }}>{entry.real_items > 0 ? entry.real_items : '—'}</span>
                        <span style={{ textAlign: 'right', color: entry.error_items > 0 ? C.redInk : C.knobOff, fontVariantNumeric: 'tabular-nums' }}>{entry.error_items > 0 ? entry.error_items : '—'}</span>
                        <span style={{ textAlign: 'center' }}>
                          {entry.status === 'ok' && <span style={{ fontSize: '10px', fontWeight: 700, color: C.green, background: C.greenBg, padding: '2px 8px', borderRadius: '4px' }}>OK</span>}
                          {entry.status === 'error' && <span style={{ fontSize: '10px', fontWeight: 700, color: C.redInk, background: C.redBg, padding: '2px 8px', borderRadius: '4px' }}>ERROR</span>}
                          {entry.status === 'empty' && <span style={{ fontSize: '10px', fontWeight: 700, color: C.faint, background: C.monoBg, padding: '2px 8px', borderRadius: '4px' }}>EMPTY</span>}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : <p style={{ fontSize: '12px', color: C.faint, padding: '16px 18px', margin: 0 }}>No scans yet.</p>)}

                {/* Activity tab */}
                {histTab === 'activity' && (
                  <div>
                    <div style={{ padding: '10px 18px', borderBottom: '1px solid #F1F5F9' }}>
                      <input type="text" placeholder="Filter by SKU, person, action, or detail…" value={auditQuery} onChange={e => setAuditQuery(e.target.value)}
                        style={{ width: '100%', maxWidth: '420px', border: '1px solid #E2E8F0', borderRadius: '6px', padding: '6px 10px', fontSize: '12px' }} />
                    </div>
                    {auditFiltered.length === 0 ? (
                      <p style={{ fontSize: '12px', color: C.faint, padding: '16px 18px', margin: 0 }}>{audit.length === 0 ? 'No onboarding actions recorded yet.' : 'No actions match that filter.'}</p>
                    ) : (
                      <div style={{ maxHeight: '420px', overflowY: 'auto' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: '150px 90px 110px 1fr 130px', gap: '10px', padding: '8px 18px', background: '#FAFAFA', fontSize: '10px', fontWeight: 700, color: C.faint, textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: '1px solid #E2E8F0', position: 'sticky', top: 0 }}>
                          <span>When</span><span>Action</span><span>SKU</span><span>Detail</span><span>By</span>
                        </div>
                        {auditFiltered.map(e => {
                          const badge = ACTION_BADGE[e.action] ?? { label: e.action, bg: C.monoBg, color: C.sub }
                          return (
                            <div key={e.id} style={{ display: 'grid', gridTemplateColumns: '150px 90px 110px 1fr 130px', gap: '10px', padding: '8px 18px', borderBottom: '1px solid #F8FAFC', fontSize: '12px', alignItems: 'center' }}>
                              <span style={{ color: C.muted, whiteSpace: 'nowrap' }}>{fmtWhen(e.created_at)}</span>
                              <span><span style={{ fontSize: '10px', fontWeight: 700, background: badge.bg, color: badge.color, padding: '2px 7px', borderRadius: '99px' }}>{badge.label}</span></span>
                              <span>{e.sku_code ? <button onClick={() => openSkuHistory(e.sku_code!)} title="View full history for this SKU" style={{ background: 'none', border: 'none', padding: 0, color: C.indigoInk, fontWeight: 600, cursor: 'pointer', fontFamily: 'ui-monospace, monospace', fontSize: '12px' }}>{e.sku_code}</button> : <span style={{ color: C.knobOff }}>—</span>}</span>
                              <span style={{ color: C.ink, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={auditSummary(e)}>{auditSummary(e)}</span>
                              <span style={{ color: '#334155', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={e.display_name ?? ''}>{e.display_name ?? 'Unknown'}</span>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

      </div>
    </>
  )
}

function Pill({ bg, color, children }: { bg: string; color: string; children: React.ReactNode }) {
  return (
    <span style={{ background: bg, color, fontSize: '11px', fontWeight: 600, padding: '2px 7px', borderRadius: '4px', whiteSpace: 'nowrap' }}>
      {children}
    </span>
  )
}
