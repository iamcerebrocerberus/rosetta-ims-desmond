import { C } from '@/lib/tokens'
import { useState } from 'react'
import { createFileRoute, Link } from '@tanstack/react-router'

// ─────────── External links ──────────────────────────────────────────────────
const SHEET_ID    = '18WUxJQZ9srms7S1oga6mrAdeH1QCA2pBsaJBUfwFRTQ'
const SHEET_URL   = `https://docs.google.com/spreadsheets/d/${SHEET_ID}/edit#gid=8428031`
const HKTV_URL    = `https://docs.google.com/spreadsheets/d/${SHEET_ID}/edit#gid=1141197811`
const DRIVE_CATS  = 'https://drive.google.com/drive/folders/1wL05wr6kazitBs8X2tLnb3RgHvWbEoTC'
const DRIVE_INV_C = 'https://drive.google.com/drive/folders/17bou7LrzUt-C91lxoS54cMSpCw_xxRda'
const DRIVE_INV_W = 'https://drive.google.com/drive/folders/1HTWsAqbolqUIHtaSbIHWPxOah_hDZBbN'

// ─────────── Catalogue Audit (snapshot: 2026-05-25) ───────────────────────────
// 47 supplier folders surveyed. Files linked by Drive file id so they open
// directly in the team's browser. Update entries here when files change.
const DRIVE_FILE = (id: string) => `https://drive.google.com/file/d/${id}/view`
const DRIVE_FOLDER = (id: string) => `https://drive.google.com/drive/folders/${id}`

type AuditDigital = { name: string; folder_id: string; best_file: string; best_file_id: string; size_mb: number; notes: string; duplicate_of?: string; flag?: 'verify' }
type AuditFlagged = { name: string; folder_id: string; best_file?: string; best_file_id?: string; size_mb?: number; notes: string }

const AUDIT_DIGITAL: AuditDigital[] = [
  { name: 'Advance Veterinary Supplies', folder_id: '1SwWdasiATiN9wgEJ7-p33O98gLIi6vPT', best_file: 'Advance Pricelist 2024_241010_091707.pdf', best_file_id: '11rRUNWwoqH4ChwBoLFczKpL4xL4AxS6C', size_mb: 1.4, notes: 'Effective Oct 2024 — consumables, diagnostics, IV therapy' },
  { name: 'Alfamedic', folder_id: '12dTkv2d2eTHcJTdcviyJyixOo1ivTs_A', best_file: 'alfamedic_HK_pricelist_edition 27_1 MAY 2025_010725_BW.pdf', best_file_id: '161bfHhp2SGXFvwsRAYPDeRWD8_-Fi2X2', size_mb: 5.8, notes: '52 pages, edition 27 · OCR confirmed working' },
  { name: 'APT', folder_id: '1E7Akj3JZpv99pZ28yC-rsSyNVj-eZpBz', best_file: 'APT price list 2025.pdf', best_file_id: '1WGW7KOmQlSK3OzcXcC_28P1eq5dzLQWu', size_mb: 0.34, notes: '2025 — products, strengths, prices, bonus terms' },
  { name: 'AVM', folder_id: '1LimkB1D0cC-Y-lrkOROJ6hoCnxvYHhu_', best_file: '(AVM) Consumable items list_20Oct2025.pdf', best_file_id: '1P_IurY7IulnBqAfXdWrseFlWaGqKnxAu', size_mb: 0.06, notes: 'Oct 2025 consumables + VetriScience/JorVet/iM3 catalogues in folder' },
  { name: 'Best Rewards Pet Products', folder_id: '1hPdgrh9EbKucoSRMTKgq7kI3v3AkUEdi', best_file: 'DR.HUGHS QUOTATION 2025 (PEE PAD).pdf', best_file_id: '1tUBXSyIqsWY8Uze_CYMPMVGCcJN52UjG', size_mb: 0.04, notes: 'Quotation only — narrow range (pee pads). Ask for full pricelist if more SKUs exist.' },
  { name: 'Christo Pharmaceutical', folder_id: '1wW-i6FfP90ICtO3iJbPWopqejeJygFkm', best_file: 'CP-CHRISTO.pdf', best_file_id: '1yiYPAiBs9aHfcS74cILLmWC2lhwHtyVt', size_mb: 2.2, notes: 'Full catalogue with HK reg numbers, prices, formulations', duplicate_of: 'Christo' },
  { name: 'DCH AURIGA (ELANCO)', folder_id: '1EPlQYSU5P1xGDAL1l-H6OPpIVA6TZsAp', best_file: 'DCH Price List - ELANCO - MAY 2026.pdf', best_file_id: '1KFjMGe0bgQPDuWjiicxQleMS4U9Hd6b_', size_mb: 0.17, notes: 'Effective May 2026 — Elanco' },
  { name: 'Europharm Laboratories', folder_id: '1VjGqC6j6jtt08zWHVNntxfO_wXocKWXZ', best_file: 'Dec 2025 EPL New price list - 121125.pdf', best_file_id: '1rdsbvaleRiosMSYH4mAXwirKoGWVNyhZ', size_mb: 0.52, notes: 'Dec 2025 doctor sector — active ingredients, tiered pricing' },
  { name: 'Flash', folder_id: '1vUm1MkkBGfSoEwTZlD5Bv1urMtGomwPc', best_file: 'Flash price list.pdf', best_file_id: '1zgKVxuc-EDwElooPw957SgQqQWNf6Xj8', size_mb: 1.5, notes: 'Tests + panels with VID codes' },
  { name: 'GSK', folder_id: '1SJ59NORh0xXeWNA0npC7vuFPhBUk_ZXG', best_file: 'GSK.pdf', best_file_id: '1EPL6kjdZm_EXjk7zghNLK2NX5vcZ75Bc', size_mb: 8.7, notes: '2026 pricelist — codes, pack sizes, forensic classifications' },
  { name: 'Hang Lung', folder_id: '1cCvU5IprSgWCQGd4PrrnOtC_EzJKhIj4', best_file: 'SKM_C650i25032410560.pdf', best_file_id: '1jxJ6WVfhCrtH0boz0b7iyJbBPSEVEIB-', size_mb: 0.3, notes: 'Aurobindo pharma pricelist — HK reg, packing, bonus terms' },
  { name: 'Happy Harvest Corp.', folder_id: '1DR9dYb84U49GlpvfpVHtrVknByShNGUi', best_file: '202501 Happy Harvest Corp. Products Price List 25年版_HK.pdf', best_file_id: '1YH7KImgLNr5TfL1s80NIcNdHxle9q1F7', size_mb: 0.39, notes: '2025 full pricelist — package sizes, end-user prices' },
  { name: 'Health Alliance', folder_id: '15G6kwlss-fNUdGvEBPmk-Aye_uHp69Wj', best_file: 'Price list as at 2025040..xls', best_file_id: '1yBwTiXEKCkMRJoe2cWhJ_8BXe7xv7vFD', size_mb: 0.75, notes: 'April 2025 — use the .xls (the 25MB HA Catalogue PDF is image-only)' },
  { name: 'HealthCare PharmaScience Ltd', folder_id: '1m9dSGbh-wY-C6E223d8Y1ufpzyx4O32U', best_file: 'Health Care 2025.pdf', best_file_id: '1qc6QkQuwLf0IU85v95B44J-kt9xgj5FP', size_mb: 0.09, notes: 'Sep 2025 — brand names, generics, packing, prices' },
  { name: 'Hind Wing', folder_id: '1z6u4-gdklAX9M0rhv0DBCiM2xHLpQoxb', best_file: 'GP (DR) price Oct 2025 with Forensic Class_Final.pdf', best_file_id: '1k9AsL6S1nrfN9Y9mzev53s9Nld0JgY_W', size_mb: 0.76, notes: 'Oct 2025 GP pricelist — packing, bonus, net price', duplicate_of: 'Hind Wing (2nd folder)' },
  { name: 'Hing Ah Pharma Company Limited', folder_id: '1Qcfv1Qq-KSHn1_lYCiIa6pkb4mvKEN-V', best_file: 'Hing Ah Pharma Company Limited.pdf', best_file_id: '1IFruFU5BgnVrDtzEZS0TU8GbmxDJtCUV', size_mb: 2.7, notes: 'Jan 2026 — compositions, strengths, prices' },
  { name: 'Hong Kong Medical Supplies', folder_id: '14oARZ5fY38S6M8L3XBpma_nqg2USHUhK', best_file: 'Hong Kong Medical Supplies.pdf', best_file_id: '1MDmlncZMLNv0I9Eo0tZxigIsEd85xRje', size_mb: 4.4, notes: 'Products with suppliers, origin, composition, packing' },
  { name: 'Jazz', folder_id: '19zdBHE2zEaDUgP4SJO2hRUtREIFw5UjY', best_file: 'Jazz price list.pdf', best_file_id: '1bxld9tUEBlyleRkVMSmxKBl4JGurIB8j', size_mb: 3.9, notes: 'Feb 2023 — Vetzyme, Kitzyme, Virkon S, SENTRY, Petromalt. Check freshness with supplier.' },
  { name: 'Kangaroo + KPN', folder_id: '1lbQmlgGcDS9ieydeUdEzkSXxTAFN4e0v', best_file: 'Blue Buffalo Wild Spirit Price List 20250710 ALL TPR.pdf', best_file_id: '1RIFe0-2CM2KTZXG49zNFnvrdRFG_VgwC', size_mb: 0.39, notes: 'Multi-brand: Blue Buffalo, Virbac, Boehringer, Purina PPVD, Cardon, PAW' },
  { name: 'Mekim', folder_id: '1sdCZbEaDW-tw725MzCC0aunfNgA9RYFf', best_file: '2026 Mar - Private Clinic Price List - Pharmaceuticals  Supportive Pharmaceuticals.xlsx', best_file_id: '1_pP4YW2hsMQsrm6XAlTGr9wSplRhuCIR', size_mb: 0.03, notes: 'Mar 2026 xlsx — material no, manufacturers, origins, prices, discounts' },
  { name: 'MSD', folder_id: '1rkguZN6N-XSvA72-B9bjiMbw9Ld-ycWZ', best_file: 'Registered Item_Order Form_202505 2.pdf', best_file_id: '15N6s8ZrwZvV60e6buwTfI4ls5A2dJpdh', size_mb: 0.52, notes: 'Order form — Bravecto, Caninsulin, Nobivac' },
  { name: 'Nordep', folder_id: '1uyWoTccQ9dgT_teOKI7BW1mhO3viBfJx', best_file: '[Promotional] Catalog_Vcheck_EN [rev.12].pdf', best_file_id: '1Qs9d55WQPlU4ZAxQhlWBAYzpHK3aQtBq', size_mb: 17.5, notes: 'Vcheck PoC diagnostics catalog' },
  { name: 'Petagon', folder_id: '1CuKq2ovK3ArG6Pyd21JgwGjbXyDHvveH', best_file: 'JPEG pricelists + Google Sheet', best_file_id: '1Ef9P73VXn1pQqu_FNaDoN0OD7xEi9InZ', size_mb: 0.44, notes: 'Mix of JPEG pricelists and a Petagon Notes Google Sheet. Vision OCR + sheet read.', flag: 'verify' },
  { name: 'Pet-Link', folder_id: '1IntWkvua3o-uncV6UPG6G76cEpDGW9HT', best_file: 'Pet=Link HK_Price_List 202506.xlsx', best_file_id: '1CiMSaLh5Si2-lFiQ1ZxO0IOyL46kWgjS', size_mb: 0.33, notes: 'Jun 2025 Jolly Pet products — codes, sizes, wholesale + retail' },
  { name: 'Pharmason', folder_id: '1ELRJyYblHeEIVNU7M9c9fZF28D09XLc7', best_file: 'Pricelist Dec 2024.pdf', best_file_id: '1Nu9JKFBacOH9s6EbMXnYEiF7d7rBZEwJ', size_mb: 1.7, notes: 'Note: folder is "Pharmason" but file is Synco (HK) Ltd ethical pricelist Dec 2024. Confirm supplier mapping.', flag: 'verify' },
  { name: "POGI'S", folder_id: '14bDJXpu_9Y4VVRhOjEe1ltajHjqGUtuy', best_file: 'Copy of Hong Kong Pricelist.xlsx', best_file_id: '1SVblE1sVcbvfg5-au0WM0LgD6m0vNHEC', size_mb: 0.01, notes: 'Jan 2025 HK pricelist — SKU IDs, case qty, wholesale + retail' },
  { name: 'Provet Kruuse', folder_id: '1LxW6jCXS140H-Mn1XkK_My4aNfK6knQV', best_file: 'Provet Hong Kong Price List - Year 2025 v2.pdf', best_file_id: '1em1hRQuaeOaajPKkoEPKYL-157mAf98y', size_mb: 0.2, notes: '2025 pricelist + consumables catalogue in folder' },
  { name: "Queen's", folder_id: '14422pjPmWRzSOsTMZBfFLMinRRWwxz5N', best_file: "Queen's.pdf", best_file_id: '126lTT5b-KWDgkVt9bLCe_rit95scD4R6', size_mb: 7.9, notes: 'Zoetis Cytopoint pricelist — narrow range. Ask if more SKUs exist.' },
  { name: 'RICH SOURCES', folder_id: '1JqahqxDXWEyLI-bfffUF1p5ipHT4Nxym', best_file: 'MONGE 2026 PRICE LIST.pdf', best_file_id: '1mYEfw_FIlIshqPRqXCrgjEDUetz623A6', size_mb: 1.4, notes: 'MONGE 2026 — dated 1 Apr 2026, MO-codes, supply + retail, UPC' },
  { name: 'RICH WINNING', folder_id: '1d7Rf_H7dnJK3Df1QA8s-_MpxehhCnNQq', best_file: '零食May2026.pdf', best_file_id: '1UNwuoOjoho8hvS_KTDiSyi3fcCOUaLYc', size_mb: 0.8, notes: 'Treats catalogue (May 2026) — product codes, weights, wholesale/retail' },
  { name: 'Sandoz', folder_id: '1otiDCPIDyMbEEZqZbOiEBN0q9LcnSKi2', best_file: 'Sandoz Catalogue Cover to Back 32pp_240625.pdf', best_file_id: '1XyMYOBXj-YLEteh_Gb_koQ8zZatJ4Hiy', size_mb: 23.7, notes: '32-page brand catalogue. Product names + strengths but minimal prices — may need vision OCR.', flag: 'verify' },
  { name: 'Santen', folder_id: '1df9WpTNSXVn7Xs6GVW21R1I4SfUPvZuK', best_file: 'Santen Price List (Private) effective 2025.10.02.pdf', best_file_id: '1Vb-rKt3OuOAbNb5k8RUdZUbsF7DQjHNN', size_mb: 0.22, notes: 'Oct 2025 private clinic — pack sizes, forensic class, list prices' },
  { name: 'Star Medical', folder_id: '1eAbMyCcIjUluYFAlkhXRWsnrl4RAzO79', best_file: 'Star Medical Supplies.pdf', best_file_id: '14AB4m-sVI3ci1C7TGBIwM2meXGvU41C5', size_mb: 14.8, notes: 'Nov 2025 — generic names, brands, packing, terms, manufacturers' },
  { name: 'TCM', folder_id: '1aUEaaBv6MQ7scxf9ZUXCLRfEahiwRiFX', best_file: 'Price List-Dr(New 15 Sep 2025).pdf', best_file_id: '1OJWDnP5IecaGBmzVmJ90PRQs62bCRSKh', size_mb: 0.64, notes: 'Sep 2025 doctor pricelist — alcohol/wipes focus' },
  { name: 'Teva', folder_id: '1HgVTmG44QHVCg98KpI6ngljWzkzVe-qM', best_file: 'Teva 2025.pdf', best_file_id: '197HMeXVaXOtmTJvYr0MNgiyNS97wbhzk', size_mb: 12.9, notes: 'Teva 2025-2026 — strengths, dosage forms, packing, therapeutic areas' },
  { name: 'Unipet House Limited', folder_id: '1zDnXcFRy_AP6u3rRpn8r5V82D9noJCdt', best_file: 'Bayer & Elanco products catalogue (2025.09.04).pdf', best_file_id: '1ncqj1NmF7uz8O36y12f3GeEB54hT_M68', size_mb: 2.8, notes: 'Sep 2025 Bayer & Elanco — product codes, pack sizes, prices' },
  { name: 'United Italian Corp.', folder_id: '1hBueLA35CbWt2xXhKIZt5OPofb7HyBvS', best_file: '2025 UI GP Price List (External).pdf', best_file_id: '1ZCyuesWqqAmu0Qygl7rWdk25c1JedK-2', size_mb: 1.7, notes: '2025 UI GP external pricelist', duplicate_of: 'United Italian Crop.(HK) Limited' },
  { name: 'Vetapet', folder_id: '1MiWeYtezrX6vbOzqx5uoQw0tiho4b2FE', best_file: 'Vet Team Catalogue B2 Dermoscent (1).pdf', best_file_id: '1-J6ncO2vmMTF9iLTcw_ktggMsUle1p4I', size_mb: 7.8, notes: 'Dermoscent catalogue — product codes, retail prices' },
  { name: 'Veterinary Specialty Pharmacy', folder_id: '1QxJBbbn7ouyCqEpFT3h1f9opwkRkosQZ', best_file: 'Formulary & Price list 2025.pdf', best_file_id: '1c0cYw6eQU3q3XieRU_JszhKk0UCBOB4C', size_mb: 1.1, notes: 'VSRx 2025 formulary — compounded drugs, strengths, quantities, expiry' },
  { name: 'Wings Pharma', folder_id: '1ULR9ncLVE2nrswAKu4qxR_uHTorIWoK-', best_file: '20260501 NEW PRICE.pdf', best_file_id: '1mmXO9VG9tc5LXl_2NLS59Wj-Os5w181I', size_mb: 1.5, notes: 'Effective May 2026 — old/new prices. Narrow scope; ask for full pricelist.' },
]

const AUDIT_SCANNED: AuditFlagged[] = [
  { name: 'Pointers Pharma', folder_id: '18z7wp-Yteuj7filssMK00Z5XhSlTYMjC', best_file: 'Copy of 腎存Nefrys_leaflet-02.jpg', best_file_id: '1OB2GJK8OkKMd0Jnpyf0Q9VNH6AzNVCJm', size_mb: 1.0, notes: 'Only marketing JPEG leaflets — no proper pricelist PDF in folder' },
]

const AUDIT_MISSING: AuditFlagged[] = [
  { name: 'Da Hon Enterprises Co. Ltd', folder_id: '1S4Gkz1AGl5wSQTd-3n01iwCC4SN3ddls', notes: 'Folder has subfolders ("2026", "Da Hon") with no files at the root level. Verify subfolders for a pricelist before requesting from supplier.' },
  { name: "Hill's", folder_id: '1OrrP4tAy1-MRUjoRuAXfA9pNfAZN1Wv4', notes: 'Root contains internal order form xlsx + subfolders (PD/VE/SD/Margins). No standalone supplier catalogue at root — ask Hill\'s rep.' },
]

const AUDIT_DUPLICATES: { primary: string; duplicate_name: string; duplicate_folder_id: string }[] = [
  { primary: 'Christo Pharmaceutical', duplicate_name: 'Christo', duplicate_folder_id: '16oTC-vhMAI_KlaE9ZIXcMAE2d_FTnPZP' },
  { primary: 'Hind Wing', duplicate_name: 'Hind Wing (2nd folder)', duplicate_folder_id: '14JwsP0a_wDodqot4Iy22ooYEfz7UnRg3' },
  { primary: 'United Italian Corp.', duplicate_name: 'United Italian Crop.(HK) Limited', duplicate_folder_id: '1-5Nml-FruzU8WZZIcb8zxlkmDYt341hw' },
]

// ─────────── Tab types ───────────────────────────────────────────────────────
type TabId = 'everyone' | 'operations' | 'accounting' | 'tech'
const TABS: { id: TabId; label: string }[] = [
  { id: 'everyone', label: 'Everyone' },
  { id: 'operations', label: 'Operations' },
  { id: 'accounting', label: 'Accounting' },
  { id: 'tech', label: 'Tech' },
]

export const Route = createFileRoute('/_authed/playbook')({ component: PlaybookPage })

function PlaybookPage() {
  const [activeTab, setActiveTab] = useState<TabId>('everyone')

  return (
      <div style={{ minHeight: '100vh' }}>

        {/* ══════════════════════════════════ HERO ══════════════════════════════════ */}
        <div style={{
          background: C.ink,
          color: 'white',
          padding: '48px 0 56px',
          marginBottom: 0,
        }}>
          <div style={{ maxWidth: '1060px', margin: '0 auto', padding: '0 24px' }}>

            {/* Badge */}
            <div style={{
              display: 'inline-block',
              fontSize: '10px',
              fontWeight: 700,
              letterSpacing: '0.08em',
              textTransform: 'uppercase' as const,
              color: '#818CF8',
              background: 'rgba(99,102,241,0.15)',
              border: '1px solid rgba(99,102,241,0.3)',
              padding: '3px 10px',
              borderRadius: '4px',
              marginBottom: '14px',
            }}>
              Rosetta IMS -- Data Confidence Project
            </div>

            {/* Headline */}
            <h1 style={{
              fontSize: '28px',
              fontWeight: 800,
              letterSpacing: '-0.5px',
              marginBottom: '6px',
              lineHeight: 1.2,
            }}>
              Every cost number <span style={{ color: '#818CF8', fontStyle: 'normal' }}>can be trusted.</span>
            </h1>
            <p style={{
              fontSize: '15px',
              color: C.faint,
              maxWidth: '600px',
              marginBottom: '36px',
              lineHeight: 1.6,
            }}>
              We&apos;re building a system where every cost number can be trusted -- so pricing,
              procurement, and reporting are based on verified facts, not guesses.
            </p>

            {/* ══ THE DATA FLOW — this is THE centerpiece ══════════════════ */}
            <div style={{ marginBottom: '40px' }}>
              <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase' as const, color: C.muted, marginBottom: '14px' }}>
                How data flows through the system
              </div>
              <div style={{ display: 'flex', alignItems: 'stretch', gap: 0 }}>

                {/* Stage 1: Source Systems */}
                <div style={{ flex: 1, background: 'rgba(148,163,184,0.1)', border: '1px solid rgba(148,163,184,0.2)', borderRadius: '10px', padding: '14px 16px' }}>
                  <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase' as const, color: C.faint, marginBottom: '8px' }}>1. Source Systems</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', fontSize: '11.5px', color: C.line }}>
                    <span>DaySmart (clinic POS)</span>
                    <span>Shopify (e-commerce)</span>
                    <span>HKTVMall (marketplace)</span>
                    <span style={{ color: '#818CF8', fontWeight: 600 }}>Supplier Catalogues (PDF/Excel)</span>
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', padding: '0 8px', fontSize: '18px', color: C.sub }}>&rarr;</div>

                {/* Stage 2: Google Sheets (temporary middle layer) */}
                <div style={{ flex: 1, background: 'rgba(251,191,36,0.08)', border: '1px dashed rgba(251,191,36,0.35)', borderRadius: '10px', padding: '14px 16px' }}>
                  <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase' as const, color: '#FBBF24', marginBottom: '8px' }}>2. Google Sheets <span style={{ fontSize: '8px', color: C.faint, fontWeight: 600, textTransform: 'none', letterSpacing: '0' }}>(temporary staging)</span></div>
                  <div style={{ fontSize: '11.5px', color: C.line, lineHeight: 1.5 }}>
                    <a href={SHEET_URL} target="_blank" rel="noreferrer" style={{ color: '#FBBF24', textDecoration: 'underline', fontWeight: 600 }}>DATABASE [SSOT]</a>
                    <br />SKU Master + HKTV Inventory
                    <br /><span style={{ fontSize: '10px', color: '#EF4444' }}>Migrating away — IMS becomes the SSOT</span>
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', padding: '0 8px', fontSize: '18px', color: C.sub }}>&rarr;</div>

                {/* Stage 3: IMS All Inventory */}
                <div style={{ flex: 1, background: 'rgba(99,102,241,0.1)', border: '1px solid rgba(99,102,241,0.25)', borderRadius: '10px', padding: '14px 16px' }}>
                  <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase' as const, color: '#818CF8', marginBottom: '8px' }}>3. Rosetta IMS &mdash; All Inventory</div>
                  <div style={{ fontSize: '11.5px', color: C.line, lineHeight: 1.5 }}>
                    Bird&apos;s eye view of all 3,044 SKUs
                    <br />Data quality graded <strong style={{ color: '#22C55E' }}>A</strong> / <strong style={{ color: '#FBBF24' }}>B</strong> / <strong style={{ color: '#EF4444' }}>C</strong>
                    <br /><span style={{ fontSize: '10px', color: C.faint }}>1,858 SKUs (61%) missing cost</span>
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', padding: '0 8px', fontSize: '18px', color: C.sub }}>&rarr;</div>

                {/* Stage 4: Data Review */}
                <div style={{ flex: 1, background: 'rgba(34,197,94,0.08)', border: '1px solid rgba(34,197,94,0.2)', borderRadius: '10px', padding: '14px 16px' }}>
                  <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase' as const, color: '#22C55E', marginBottom: '8px' }}>4. Data Review</div>
                  <div style={{ fontSize: '11.5px', color: C.line, lineHeight: 1.5 }}>
                    <span style={{ color: '#22C55E', fontWeight: 600 }}>OCR extraction</span> (live)
                    <br />Human-in-the-loop review
                    <br />3-way match (accounting automation)
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', padding: '0 8px', fontSize: '18px', color: C.sub }}>&rarr;</div>

                {/* Stage 5: Export back */}
                <div style={{ flex: 1, background: 'rgba(148,163,184,0.1)', border: '1px solid rgba(148,163,184,0.2)', borderRadius: '10px', padding: '14px 16px' }}>
                  <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase' as const, color: C.faint, marginBottom: '8px' }}>5. Export to Sources</div>
                  <div style={{ fontSize: '11.5px', color: C.line, lineHeight: 1.5 }}>
                    CSV &rarr; DaySmart
                    <br />CSV &rarr; Shopify
                    <br />3-way match &rarr; QuickBooks
                    <br /><span style={{ fontSize: '10px', color: '#EF4444', fontWeight: 600 }}>IMS never writes back to Sheet</span>
                  </div>
                </div>
              </div>
            </div>

            {/* ── Confidence journey (secondary) ─────────────────────────── */}
            <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase' as const, color: C.muted, marginBottom: '14px' }}>
              Data confidence journey
            </div>

            {/* ── Journey: 30 / 80 / 100 ────────────────────────────────── */}
            <div style={{ display: 'flex', alignItems: 'stretch', gap: 0, position: 'relative' }}>

              {/* 30% */}
              <div style={{
                flex: 1,
                position: 'relative',
                padding: '28px 24px 22px',
                borderRadius: '12px',
                minHeight: '170px',
                display: 'flex',
                flexDirection: 'column',
                background: 'rgba(239,68,68,0.12)',
                border: '1px solid rgba(239,68,68,0.25)',
              }}>
                <div style={{ fontSize: '48px', fontWeight: 800, lineHeight: 1, letterSpacing: '-2px', marginBottom: '6px', color: '#EF4444' }}>30%</div>
                <div style={{ fontSize: '11px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.06em', marginBottom: '10px', color: '#FCA5A5' }}>Where we were</div>
                <span style={{ display: 'inline-block', fontSize: '9px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.06em', padding: '2px 8px', borderRadius: '3px', marginBottom: '10px', width: 'fit-content', background: '#7F1D1D', color: '#FCA5A5' }}>Yesterday</span>
                <p style={{ fontSize: '12.5px', color: C.knobOff, lineHeight: 1.6, flex: 1 }}>
                  Supplier catalogues had errors we couldn&apos;t catch at volume. Every purchase order required manual spot-checking against PDFs. We&apos;ve had double-payments, missed payments, and pricing set from outdated cost data.
                </p>
              </div>

              {/* Arrow */}
              <div style={{ display: 'flex', alignItems: 'center', padding: '0 6px', fontSize: '24px', color: C.sub, flexShrink: 0 }}>&rarr;</div>

              {/* 80% */}
              <div style={{
                flex: 1,
                position: 'relative',
                padding: '28px 24px 22px',
                borderRadius: '12px',
                minHeight: '170px',
                display: 'flex',
                flexDirection: 'column',
                background: 'rgba(99,102,241,0.12)',
                border: '1px solid rgba(99,102,241,0.3)',
              }}>
                <div style={{ fontSize: '48px', fontWeight: 800, lineHeight: 1, letterSpacing: '-2px', marginBottom: '6px', color: '#818CF8' }}>80%</div>
                <div style={{ fontSize: '11px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.06em', marginBottom: '10px', color: '#A5B4FC' }}>OCR + human review</div>
                <span style={{ display: 'inline-block', fontSize: '9px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.06em', padding: '2px 8px', borderRadius: '3px', marginBottom: '10px', width: 'fit-content', background: 'rgba(99,102,241,0.25)', color: C.indigoLine }}>Live now</span>
                <p style={{ fontSize: '12.5px', color: C.knobOff, lineHeight: 1.6, flex: 1 }}>
                  AI reads our supplier catalogues and extracts costs, pack sizes, and supplier codes automatically.
                  Our team reviews matches in a bank-reconciliation-style UI. <strong style={{ color: 'white', fontWeight: 600 }}>1,014 items</strong> already
                  processed across <strong style={{ color: 'white', fontWeight: 600 }}>17 suppliers</strong>, at a total AI cost of about <strong style={{ color: 'white', fontWeight: 600 }}>~$8</strong>.
                  Supplier SKU capture <strong style={{ color: 'white', fontWeight: 600 }}>compounds year-over-year</strong>.
                </p>
              </div>

              {/* Arrow */}
              <div style={{ display: 'flex', alignItems: 'center', padding: '0 6px', fontSize: '24px', color: C.sub, flexShrink: 0 }}>&rarr;</div>

              {/* 100% */}
              <div style={{
                flex: 1,
                position: 'relative',
                padding: '28px 24px 22px',
                borderRadius: '12px',
                minHeight: '170px',
                display: 'flex',
                flexDirection: 'column',
                background: 'rgba(34,197,94,0.1)',
                border: '1px solid rgba(34,197,94,0.25)',
              }}>
                <div style={{ fontSize: '48px', fontWeight: 800, lineHeight: 1, letterSpacing: '-2px', marginBottom: '6px', color: '#22C55E' }}>100%</div>
                <div style={{ fontSize: '11px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.06em', marginBottom: '10px', color: '#86EFAC' }}>3-way match</div>
                <span style={{ display: 'inline-block', fontSize: '9px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.06em', padding: '2px 8px', borderRadius: '3px', marginBottom: '10px', width: 'fit-content', background: 'rgba(34,197,94,0.15)', color: '#86EFAC' }}>Scoped</span>
                <p style={{ fontSize: '12.5px', color: C.knobOff, lineHeight: 1.6, flex: 1 }}>
                  3-way matching software will match purchase orders against delivery notes and invoices <strong style={{ color: 'white', fontWeight: 600 }}>automatically</strong>.
                  Sam&apos;s team validates before QuickBooks entry. Every cost confirmed against a real document -- not a catalogue, not a memory, an actual invoice.
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* ══════════════════════════════════ TABS ══════════════════════════════════ */}
        <div style={{ maxWidth: '1060px', margin: '0 auto', padding: '0 24px 60px' }}>

          {/* Tab bar */}
          <div style={{
            display: 'flex',
            gap: 0,
            background: C.monoBg,
            borderRadius: '10px 10px 0 0',
            padding: '4px 4px 0',
            marginTop: 0,
          }}>
            {TABS.map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                style={{
                  flex: 1,
                  padding: '11px 16px',
                  fontSize: '13px',
                  fontWeight: 600,
                  color: activeTab === tab.id ? C.ink : C.muted,
                  background: activeTab === tab.id ? 'white' : 'transparent',
                  border: 'none',
                  borderRadius: '8px 8px 0 0',
                  cursor: 'pointer',
                  textAlign: 'center' as const,
                  transition: 'all 0.15s',
                  boxShadow: activeTab === tab.id ? '0 -1px 3px rgba(15,23,42,0.06)' : 'none',
                }}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Tab content wrapper */}
          <div style={{
            background: 'white',
            border: '1px solid #E2E8F0',
            borderTop: 'none',
            borderRadius: '0 0 10px 10px',
            padding: '28px 32px',
          }}>

            {/* ═══════════════════ EVERYONE TAB ═══════════════════ */}
            {activeTab === 'everyone' && (
              <div>
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>The mission</p>
                <h2 style={{ fontSize: '16px', fontWeight: 700, color: C.ink, marginBottom: '6px' }}>Trust the numbers</h2>
                <p style={{ fontSize: '13px', color: C.muted, marginBottom: '20px', lineHeight: 1.6 }}>
                  Right now, many SKU costs are missing, wrong, or unverified. That means every margin check,
                  every reorder decision, and every GP report could be quietly wrong. This project fixes that --
                  starting with the highest-sales products, because a data error on something we sell 200 units
                  of every month costs far more than one we sell twice a year.
                </p>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* What's live / what's coming */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>What&apos;s live / what&apos;s coming</p>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: '10px' }}>

                  <StatusCard status="live" tag="LIVE" title="OCR Catalogue Scanner">
                    1,014 items extracted from 17 suppliers via Claude AI. Xero-style reconciliation UI
                    with side-by-side diff cards, field-level highlighting, and bulk approve/reject.
                    7 Alfamedic matches approved (supplier SKUs captured).
                    Supplier SKU pairing captured on approval -- enables instant matching on future catalogue editions.
                  </StatusCard>

                  <StatusCard status="scoped" tag="SCOPED" title="3-Way Match + QuickBooks">
                    Scoped 25 May. PO matched against delivery note matched against invoice.
                    His internal OCR handles scanned delivery notes. Sam&apos;s junior accountant validates
                    before final QuickBooks entry. Eliminates manual data keying.
                  </StatusCard>

                  <StatusCard status="planned" tag="PLANNED" title="Automated Stock Sync, Sales Velocity Pipeline, Channel Writebacks">
                    Replace manual DaySmart/Warehouse CSV exports with scheduled API pulls.
                    Transaction-level sales data for accurate WOC calculations and smarter reorder triggers.
                  </StatusCard>

                </div>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* Key numbers */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Key numbers</p>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '12px' }}>
                  <NumberCard value="3,044" label="Active SKUs in IMS" />
                  <NumberCard value="1,858" label="Missing cost data (61%)" />
                  <NumberCard value="43" label="Digital catalogues audited" />
                  <NumberCard value="~$8" label="Total API cost for all extractions" />
                </div>
              </div>
            )}

            {/* ═══════════════════ OPERATIONS TAB ═══════════════════ */}
            {activeTab === 'operations' && (
              <div>
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>For the operations team</p>
                <h2 style={{ fontSize: '16px', fontWeight: 700, color: C.ink, marginBottom: '6px' }}>Your daily workflow is changing. Here&apos;s what&apos;s different.</h2>
                <p style={{ fontSize: '13px', color: C.muted, marginBottom: '20px', lineHeight: 1.6 }}>
                  The goal is not to replace people -- it&apos;s to remove the parts of your job that are
                  tedious data-keying so you can focus on the parts that need human judgment.
                </p>

                {/* Before / After */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '24px' }}>
                  <div>
                    <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.redInk, marginBottom: '12px' }}>Old flow (being retired)</p>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                      <FlowBox variant="old">Warehouse scans delivery notes</FlowBox>
                      <FlowArrow variant="old" />
                      <FlowBox variant="old">Uploads PDFs to Google Drive</FlowBox>
                      <FlowArrow variant="old" />
                      <FlowBox variant="old">Manual data keying from Drive into Rosetta Sheet</FlowBox>
                      <FlowArrow variant="old" />
                      <FlowBox variant="old">Manual entry into QuickBooks</FlowBox>
                    </div>
                  </div>
                  <div>
                    <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.green, marginBottom: '12px' }}>New flow (being built)</p>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                      <FlowBox variant="new">Warehouse scans delivery notes</FlowBox>
                      <FlowArrow variant="new" />
                      <FlowBox variant="new">Uploads PDFs to Google Drive</FlowBox>
                      <FlowArrow variant="new" />
                      <div style={{
                        padding: '8px 14px',
                        borderRadius: '6px',
                        fontSize: '12px',
                        fontWeight: 600,
                        textAlign: 'center' as const,
                        flex: 1,
                        background: '#DBEAFE',
                        color: '#1E40AF',
                        border: '1px solid #93C5FD',
                      }}>
                        The 3-way match OCR auto-extracts + 3-way matches
                      </div>
                      <FlowArrow variant="new" />
                      <FlowBox variant="new">Junior accountant validates &rarr; QuickBooks</FlowBox>
                    </div>
                  </div>
                </div>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* Who does what now */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Who does what now</p>
                <StepList>
                  <StepItem>
                    <strong style={{ color: C.ink }}>Warehouse team</strong> -- Scans delivery notes at the HK warehouse.
                    Uploads to the shared Google Drive folder. <em>This step stays the same.</em>{' '}
                    Make sure scans are clear and complete -- The 3-way match OCR needs readable text.
                  </StepItem>
                  <StepItem>
                    <strong style={{ color: C.ink }}>Catalogue review queue</strong> -- When a new supplier price list arrives,
                    review it in the <a href="http://localhost:3001/catalogues" style={{ color: C.indigo, textDecoration: 'none' }}>Catalogue Review Queue</a> in IMS.
                    The AI has already extracted the data; your job is to approve, reject, or correct matches.
                    Start with 99%+ confidence items (green) -- those are near-certain and fast to clear.
                  </StepItem>
                  <StepItem>
                    <strong style={{ color: C.ink }}>QuickBooks entry</strong> -- Being automated by the 3-way match system.
                    During the transition, continue manual entry for any items that fail automated matching.
                    You will be notified which items need manual handling.
                  </StepItem>
                </StepList>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* What's being automated */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>What manual steps are being automated</p>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '24px' }}>
                  <div style={{ background: C.badBg, border: '1px solid #FECACA', borderRadius: '8px', padding: '16px 18px' }}>
                    <p style={{ fontSize: '12px', fontWeight: 700, color: C.redInk, marginBottom: '4px' }}>
                      Manual catalogue reading &rarr; eliminated
                    </p>
                    <p style={{ fontSize: '11.5px', color: C.bad, lineHeight: 1.6 }}>
                      Claude AI now reads all 43 supplier PDFs. Cost, SKU, pack size, and bulk-buy tiers
                      extracted automatically. Pipeline is maintained centrally.
                    </p>
                  </div>
                  <div style={{ background: C.badBg, border: '1px solid #FECACA', borderRadius: '8px', padding: '16px 18px' }}>
                    <p style={{ fontSize: '12px', fontWeight: 700, color: C.redInk, marginBottom: '4px' }}>
                      Manual data keying to Sheet + QuickBooks &rarr; automated
                    </p>
                    <p style={{ fontSize: '11.5px', color: C.bad, lineHeight: 1.6 }}>
                      The 3-way match system handles PO/DN/invoice reconciliation and outputs to QuickBooks.
                      Sam&apos;s junior accountant validates before final entry.
                    </p>
                  </div>
                </div>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* FAQ */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>FAQ</p>
                <div style={{ marginBottom: '16px' }}>
                  <p style={{ fontSize: '13px', fontWeight: 600, color: C.ink, marginBottom: '4px' }}>What if the AI gets a price wrong?</p>
                  <p style={{ fontSize: '12.5px', color: C.muted, lineHeight: 1.6 }}>
                    That&apos;s exactly what the review queue is for. Every AI-extracted cost sits in &quot;pending&quot; until a human confirms it.
                    Nothing flows to pricing or QuickBooks without a person signing off.
                  </p>
                </div>
                <div style={{ marginBottom: '16px' }}>
                  <p style={{ fontSize: '13px', fontWeight: 600, color: C.ink, marginBottom: '4px' }}>How long does a catalogue take to process?</p>
                  <p style={{ fontSize: '12.5px', color: C.muted, lineHeight: 1.6 }}>
                    Upload to review-ready: under 60 seconds for most PDFs. Review itself depends on the catalogue size --
                    a 50-item list takes about 10 minutes once you know the UI.
                  </p>
                </div>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* Pre-OCR Catalogue Audit */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Pre-OCR Catalogue Audit</p>
                <p style={{ fontSize: '13px', color: C.sub, lineHeight: 1.7, marginBottom: '14px' }}>
                  Before burning API credits running OCR on every supplier catalogue, we audited all 47 supplier folders in Drive
                  to confirm which ones have a usable digital catalogue vs which need follow-up with the supplier.
                </p>

                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '20px' }}>
                  <Tag bg={C.greenBg} color={C.green}>{AUDIT_DIGITAL.length} ready to OCR</Tag>
                  <Tag bg={C.warnBg} color={C.amberInk}>{AUDIT_SCANNED.length} scanned -- ask supplier for digital</Tag>
                  <Tag bg={C.redBg} color={C.redInk}>{AUDIT_MISSING.length} no catalogue -- ask supplier</Tag>
                  <Tag bg={C.monoBg} color={C.sub}>{AUDIT_DUPLICATES.length} duplicate folders to clean up</Tag>
                </div>

                {/* Green list */}
                <div style={{ border: '1px solid #BBF7D0', borderRadius: '6px', overflow: 'hidden', marginBottom: '16px' }}>
                  <div style={{ display: 'grid', gridTemplateColumns: '200px 1fr 60px 60px', gap: '10px', background: '#F0FDF4', padding: '7px 12px', borderBottom: '1px solid #BBF7D0' }}>
                    {['Supplier', 'Best file', 'Size', 'Open'].map(h => (
                      <span key={h} style={{ fontSize: '10px', fontWeight: 700, color: C.green, textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>{h}</span>
                    ))}
                  </div>
                  {AUDIT_DIGITAL.map((s, i) => (
                    <div key={s.folder_id} style={{
                      display: 'grid', gridTemplateColumns: '200px 1fr 60px 60px',
                      gap: '10px', padding: '8px 12px', alignItems: 'start',
                      background: i % 2 === 0 ? 'white' : '#FAFAFA',
                      borderBottom: i < AUDIT_DIGITAL.length - 1 ? '1px solid #F1F5F9' : 'none',
                    }}>
                      <div>
                        <p style={{ fontSize: '12px', fontWeight: 600, color: C.ink, marginBottom: '1px' }}>{s.name}</p>
                        {s.duplicate_of && <p style={{ fontSize: '10px', color: C.amberInk, fontStyle: 'italic' }}>also in &ldquo;{s.duplicate_of}&rdquo; folder</p>}
                        {s.flag === 'verify' && <p style={{ fontSize: '10px', color: C.amberInk, fontWeight: 600 }}>verify</p>}
                      </div>
                      <div>
                        <p style={{ fontSize: '11px', color: C.sub, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.best_file}</p>
                        <p style={{ fontSize: '10px', color: C.faint, marginTop: '2px', lineHeight: 1.4 }}>{s.notes}</p>
                      </div>
                      <span style={{ fontSize: '11px', color: C.sub, fontVariantNumeric: 'tabular-nums' }}>{s.size_mb} MB</span>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                        <a href={DRIVE_FILE(s.best_file_id)} target="_blank" rel="noreferrer" style={{ fontSize: '10.5px', fontWeight: 600, color: C.green, textDecoration: 'none' }}>file</a>
                        <a href={DRIVE_FOLDER(s.folder_id)} target="_blank" rel="noreferrer" style={{ fontSize: '10.5px', color: C.faint, textDecoration: 'none' }}>folder</a>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Scanned list */}
                {AUDIT_SCANNED.length > 0 && (
                  <div style={{ border: '1px solid #FDE68A', borderRadius: '6px', overflow: 'hidden', marginBottom: '16px' }}>
                    <div style={{ background: '#FFFBEB', padding: '7px 12px', borderBottom: '1px solid #FDE68A' }}>
                      <span style={{ fontSize: '10px', fontWeight: 700, color: C.amberInk, textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Scanned -- ask supplier for digital ({AUDIT_SCANNED.length})</span>
                    </div>
                    {AUDIT_SCANNED.map((s, i) => (
                      <div key={s.folder_id} style={{
                        display: 'grid', gridTemplateColumns: '200px 1fr 60px',
                        gap: '10px', padding: '10px 12px', alignItems: 'start',
                        background: i % 2 === 0 ? '#FFFBEB' : '#FEFCE8',
                      }}>
                        <p style={{ fontSize: '12px', fontWeight: 600, color: '#78350F' }}>{s.name}</p>
                        <div>
                          <p style={{ fontSize: '11px', color: '#78350F' }}>{s.best_file ?? '--'}</p>
                          <p style={{ fontSize: '10px', color: C.amberInk, marginTop: '2px', lineHeight: 1.4 }}>{s.notes}</p>
                        </div>
                        <a href={DRIVE_FOLDER(s.folder_id)} target="_blank" rel="noreferrer" style={{ fontSize: '10.5px', fontWeight: 600, color: C.amberInk, textDecoration: 'none' }}>folder</a>
                      </div>
                    ))}
                  </div>
                )}

                {/* Missing list */}
                <div style={{ border: '1px solid #FECACA', borderRadius: '6px', overflow: 'hidden', marginBottom: '16px' }}>
                  <div style={{ background: C.badBg, padding: '7px 12px', borderBottom: '1px solid #FECACA' }}>
                    <span style={{ fontSize: '10px', fontWeight: 700, color: C.redInk, textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>No catalogue -- ask supplier ({AUDIT_MISSING.length})</span>
                  </div>
                  {AUDIT_MISSING.map((s, i) => (
                    <div key={s.folder_id} style={{
                      display: 'grid', gridTemplateColumns: '200px 1fr 60px',
                      gap: '10px', padding: '10px 12px', alignItems: 'start',
                      background: i % 2 === 0 ? C.badBg : '#FEFEFE',
                      borderBottom: i < AUDIT_MISSING.length - 1 ? '1px solid #FECACA' : 'none',
                    }}>
                      <p style={{ fontSize: '12px', fontWeight: 600, color: C.redInk }}>{s.name}</p>
                      <p style={{ fontSize: '11px', color: C.redInk, lineHeight: 1.5 }}>{s.notes}</p>
                      <a href={DRIVE_FOLDER(s.folder_id)} target="_blank" rel="noreferrer" style={{ fontSize: '10.5px', fontWeight: 600, color: C.redInk, textDecoration: 'none' }}>folder</a>
                    </div>
                  ))}
                </div>

                {/* Duplicate folders */}
                {AUDIT_DUPLICATES.length > 0 && (
                  <div style={{ border: '1px solid #E2E8F0', borderRadius: '6px', overflow: 'hidden', marginBottom: '16px' }}>
                    <div style={{ background: C.wash, padding: '7px 12px', borderBottom: '1px solid #E2E8F0' }}>
                      <span style={{ fontSize: '10px', fontWeight: 700, color: C.sub, textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Duplicate folders to clean up ({AUDIT_DUPLICATES.length})</span>
                    </div>
                    {AUDIT_DUPLICATES.map((d, i) => (
                      <div key={d.duplicate_folder_id} style={{
                        display: 'grid', gridTemplateColumns: '1fr auto',
                        gap: '12px', padding: '8px 12px', alignItems: 'center',
                        background: i % 2 === 0 ? 'white' : '#FAFAFA',
                        borderBottom: i < AUDIT_DUPLICATES.length - 1 ? '1px solid #F1F5F9' : 'none',
                      }}>
                        <p style={{ fontSize: '11.5px', color: C.sub }}>
                          <strong style={{ color: C.ink }}>{d.duplicate_name}</strong>
                          {' '}is a duplicate of{' '}
                          <strong style={{ color: C.ink }}>{d.primary}</strong>
                          {' '}-- consolidate into one folder
                        </p>
                        <a href={DRIVE_FOLDER(d.duplicate_folder_id)} target="_blank" rel="noreferrer" style={{ fontSize: '10.5px', fontWeight: 600, color: C.indigo, textDecoration: 'none' }}>open dupe</a>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* ═══════════════════ ACCOUNTING TAB ═══════════════════ */}
            {activeTab === 'accounting' && (
              <div>
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>For finance + accounting</p>
                <h2 style={{ fontSize: '16px', fontWeight: 700, color: C.ink, marginBottom: '6px' }}>Cost confidence you can close books with</h2>
                <p style={{ fontSize: '13px', color: C.muted, marginBottom: '20px', lineHeight: 1.6 }}>
                  Every cost figure in the system now carries a confidence level.
                  The hierarchy determines what gets used for GP calculations and what needs further verification.
                </p>

                {/* Confidence hierarchy */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Cost confidence hierarchy (highest to lowest)</p>
                <div style={{ marginBottom: '24px' }}>
                  <ConfTier rank={1} bg="#F0FDF4" border="#BBF7D0" rankBg="#16A34A" labelColor={C.green} descColor={C.muted}
                    label="catalogue"
                    desc="Extracted from the live supplier price list via OCR, then confirmed by a reviewer. Top tier — protected from Sheet re-syncs, because a human has signed off on it against the current catalogue."
                  />
                  <ConfTier rank={2} bg="#EFF6FF" border="#BFDBFE" rankBg="#2563EB" labelColor="#1E40AF" descColor={C.muted}
                    label="invoice_matched"
                    desc="Verified against the actual supplier invoice via 3-way match. The gold standard for QuickBooks entry; sits just below a freshly reviewed catalogue cost."
                  />
                  <ConfTier rank={3} bg={C.wash} border={C.line} rankBg={C.muted} labelColor={C.sub} descColor={C.muted}
                    label="manual"
                    desc="Human-entered, no source document linked. Treat with caution. Common for legacy data migrated from spreadsheets."
                  />
                  <ConfTier rank={4} bg={C.badBg} border="#FECACA" rankBg="#DC2626" labelColor={C.redInk} descColor={C.muted}
                    label="unknown"
                    desc="No cost data at all. GP cannot be computed. These products need immediate attention -- either locate a catalogue or request a quote from the supplier."
                  />
                </div>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* 3-way match */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>How 3-way matching works</p>
                <div style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '16px',
                  padding: '20px', background: C.wash, border: '1px solid #E2E8F0', borderRadius: '8px', margin: '16px 0',
                }}>
                  <div style={{ textAlign: 'center' as const, padding: '12px 18px', borderRadius: '8px', fontSize: '12px', fontWeight: 700, minWidth: '120px', background: C.primaryBg, border: '2px solid #818CF8', color: '#3730A3' }}>
                    Purchase Order
                  </div>
                  <div style={{ fontSize: '22px', color: C.faint }}>&harr;</div>
                  <div style={{ textAlign: 'center' as const, padding: '12px 18px', borderRadius: '8px', fontSize: '12px', fontWeight: 700, minWidth: '120px', background: '#FFFBEB', border: '2px solid #F59E0B', color: '#78350F' }}>
                    Delivery Note
                  </div>
                  <div style={{ fontSize: '22px', color: C.faint }}>&harr;</div>
                  <div style={{ textAlign: 'center' as const, padding: '12px 18px', borderRadius: '8px', fontSize: '12px', fontWeight: 700, minWidth: '120px', background: '#F0FDF4', border: '2px solid #22C55E', color: '#14532D' }}>
                    Supplier Invoice
                  </div>
                </div>
                <p style={{ fontSize: '12px', color: C.muted, textAlign: 'center' as const, lineHeight: 1.6, marginBottom: '20px' }}>
                  When all three documents agree on quantity and price, the cost is promoted to{' '}
                  <code style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '0.85em', background: C.monoBg, padding: '1px 5px', borderRadius: '3px' }}>invoice_matched</code> -- an invoice-verified tier, just below a freshly reviewed catalogue cost.<br />
                  The 3-way matching software OCRs scanned delivery notes and outputs matched line items to QuickBooks.
                </p>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* QuickBooks + month-end */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '20px' }}>
                  <div>
                    <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>QuickBooks integration scope</p>
                    <ul style={{ paddingLeft: '18px', fontSize: '12px', color: C.sub, lineHeight: 1.8 }}>
                      <li>QuickBooks API integration being scoped with finance team</li>
                      <li>Matched line items auto-posted (behind human approval)</li>
                      <li>Unmatched items flagged for manual review by junior accountant</li>
                      <li>Accounts team retains veto before any QB entry is finalized</li>
                    </ul>
                  </div>
                  <div>
                    <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Month-end closing impact</p>
                    <ul style={{ paddingLeft: '18px', fontSize: '12px', color: C.sub, lineHeight: 1.8 }}>
                      <li>Margin analysis moves from &quot;after the fact&quot; to near real-time</li>
                      <li>Cost discrepancies flagged during the month, not at close</li>
                      <li>GP floor breaches visible in IMS before they hit the books</li>
                      <li>Junior accountant validates daily, reducing month-end crunch</li>
                    </ul>
                  </div>
                </div>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* GP floors */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>GP floor rules by category</p>
                <div style={{ background: 'white', border: '1px solid #E2E8F0', borderRadius: '8px', padding: '12px 16px' }}>
                  {[
                    { cat: 'Medicine', val: '70%', color: C.redInk },
                    { cat: 'Preventative', val: '40%', color: C.amberInk },
                    { cat: 'Supplement', val: '40%', color: C.amberInk },
                    { cat: 'Pet Hygiene / Shampoo', val: '40%', color: C.amberInk },
                    { cat: 'Food / Cat Litter / Toys', val: '35%', color: '#1E40AF' },
                    { cat: 'Not-For-Sale', val: '--', color: C.faint },
                  ].map((r, i) => (
                    <div key={r.cat} style={{
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      padding: '6px 12px', borderRadius: '4px', fontSize: '12px',
                      background: i % 2 === 0 ? C.wash : 'white',
                    }}>
                      <span style={{ fontWeight: 600, color: C.ink }}>{r.cat}</span>
                      <span style={{ fontWeight: 700, fontVariantNumeric: 'tabular-nums', color: r.color }}>{r.val}</span>
                    </div>
                  ))}
                </div>
                <p style={{ fontSize: '11px', color: C.faint, marginTop: '6px' }}>
                  GP% = (Selling Price - Cost) / Selling Price x 100.
                  Any product priced below its category floor triggers a &quot;Below Margin&quot; flag in IMS.
                </p>
              </div>
            )}

            {/* ═══════════════════ TECH TAB ═══════════════════ */}
            {activeTab === 'tech' && (
              <div>
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>For the development team</p>
                <h2 style={{ fontSize: '16px', fontWeight: 700, color: C.ink, marginBottom: '6px' }}>System architecture at a glance</h2>
                <p style={{ fontSize: '13px', color: C.muted, marginBottom: '20px', lineHeight: 1.6 }}>
                  Two services, one SQLite database, no external infrastructure beyond Google Drive and Claude API.
                  This is intentionally simple -- we are not building a platform.
                </p>

                {/* Architecture diagram */}
                <div style={{ background: 'white', border: '1px solid #E2E8F0', borderRadius: '8px', padding: '20px 24px', marginBottom: '20px' }}>

                  <ArchLayer bg={C.wash} border={C.knobOff} labelColor={C.muted} label="Source Systems">
                    <ArchBox border={C.knobOff} color={C.ink}>DaySmart POS</ArchBox>
                    <ArchBox border={C.knobOff} color={C.ink}>Shopify</ArchBox>
                    <ArchBox border={C.knobOff} color={C.ink}>HKTVMall</ArchBox>
                    <ArchBox border={C.knobOff} color={C.ink}>Google Sheets SSOT</ArchBox>
                  </ArchLayer>

                  <div style={{ textAlign: 'center' as const, fontSize: '14px', color: C.faint, padding: '4px 0' }}>&darr; Sheet sync + CSV imports (one-way)</div>

                  <ArchLayer bg={C.primaryBg} border={C.indigoLine} labelColor={C.indigoInk} label="Backend -- FastAPI + SQLite (port 8001)">
                    <ArchBox border={C.indigoLine} color="#3730A3">SKU Master (3,044 items)</ArchBox>
                    <ArchBox border={C.indigoLine} color="#3730A3">Pricing Engine</ArchBox>
                    <ArchBox border={C.indigoLine} color="#3730A3">OCR Extraction Service</ArchBox>
                    <ArchBox border={C.indigoLine} color="#3730A3">Catalogue Matcher</ArchBox>
                  </ArchLayer>

                  <div style={{ textAlign: 'center' as const, fontSize: '14px', color: C.faint, padding: '4px 0' }}>&darr; REST API</div>

                  <ArchLayer bg="#F0FDF4" border="#BBF7D0" labelColor={C.green} label="Frontend -- Next.js 16 + TypeScript (port 3001)">
                    <ArchBox border="#BBF7D0" color={C.green}>All Inventory</ArchBox>
                    <ArchBox border="#BBF7D0" color={C.green}>Catalogue Review</ArchBox>
                    <ArchBox border="#BBF7D0" color={C.green}>Data Review</ArchBox>
                    <ArchBox border="#BBF7D0" color={C.green}>This Page</ArchBox>
                  </ArchLayer>
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '20px' }}>

                  {/* Key endpoints */}
                  <div>
                    <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Key API endpoints</p>
                    <div style={{ background: 'white', border: '1px solid #E2E8F0', borderRadius: '8px', padding: '12px 16px' }}>
                      <EndpointGrid>
                        <EndpointRow method="POST" path="/catalogues/ingestions" desc="Upload PDF/Excel/CSV -- queued evidence-first pipeline (raw, staging, intermediate, serving)" />
                        <EndpointRow method="GET" path="/catalogues/{id}/items" desc="List extracted items for a catalogue, filterable by review_status" />
                        <EndpointRow method="GET" path="/catalogues/queue/pending" desc="Cross-catalogue pending queue, ordered by confidence score" />
                        <EndpointRow method="POST" path="/catalogues/items/{id}/match" desc="Confirm match to existing internal SKU" />
                        <EndpointRow method="POST" path="/catalogues/items/bulk-match" desc="Batch-confirm multiple matches in one call" />
                        <EndpointRow method="GET" path="/api/products" desc="Full inventory with cost confidence" />
                        <EndpointRow method="PATCH" path="/api/products/{sku}" desc="Update cost, category, supplier, UOM" />
                        <EndpointRow method="GET" path="/api/pricing" desc="GP matrix across all channels" />
                      </EndpointGrid>
                    </div>
                  </div>

                  {/* Database overview */}
                  <div>
                    <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Database schema (SQLite)</p>
                    <div style={{ background: 'white', border: '1px solid #E2E8F0', borderRadius: '8px', padding: '12px 16px' }}>
                      <EndpointGrid>
                        <EndpointRow method="" path="products" desc="SKU master -- costs, categories, suppliers, UOM" />
                        <EndpointRow method="" path="product_suppliers" desc="Supplier-specific pricing per SKU -- basic_cost, catalogue_cost, bulk tiers" />
                        <EndpointRow method="" path="catalogue_imports" desc="Each uploaded catalogue file -- supplier, filename, status" />
                        <EndpointRow method="" path="catalogue_items" desc="Individual extracted line items -- review_status, confidence, matched product" />
                        <EndpointRow method="" path="stock_levels" desc="Per-location quantities + WOC" />
                        <EndpointRow method="" path="users" desc="JWT auth, roles, edit attribution" />
                      </EndpointGrid>
                    </div>
                  </div>
                </div>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* OCR pipeline */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>OCR pipeline</p>
                <StepList>
                  <StepItem><strong style={{ color: C.ink }}>Text extraction:</strong> pypdf extracts text from PDF pages. Scanned (image-only) PDFs fall through to Claude Vision.</StepItem>
                  <StepItem><strong style={{ color: C.ink }}>Chunking:</strong> Large documents split into 14K-character chunks with 500-char overlap. Chunk boundaries snap to newlines to avoid splitting line items.</StepItem>
                  <StepItem><strong style={{ color: C.ink }}>AI extraction:</strong> Each chunk sent to Claude Haiku with a structured extraction prompt. Output: JSON array of product objects.</StepItem>
                  <StepItem><strong style={{ color: C.ink }}>Dedup:</strong> Cross-chunk deduplication on (supplier_sku, description[:40]) to prevent double-counting at overlap boundaries.</StepItem>
                  <StepItem><strong style={{ color: C.ink }}>Review queue:</strong> Items inserted with review_status=&apos;pending&apos;. Sorted by confidence_score descending.</StepItem>
                </StepList>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* Matching algorithm */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Matching algorithm (3-pass)</p>
                <StepList>
                  <StepItem><strong style={{ color: C.ink }}>Barcode exact match</strong> -- looks up barcode in product_suppliers. Confidence: 0.99.</StepItem>
                  <StepItem><strong style={{ color: C.ink }}>Supplier SKU exact match</strong> -- matches on (supplier_id, supplier_sku). Confidence: 0.95.</StepItem>
                  <StepItem><strong style={{ color: C.ink }}>Fuzzy name match</strong> -- word overlap (threshold 65%), boosted by +0.10 if units_per_pack matches and +0.10 if cost is within 15% of existing. Top 3 returned.</StepItem>
                </StepList>

                <div style={{ height: '1px', background: C.line, margin: '20px 0' }} />

                {/* Integration points */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Integration points</p>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '12px', marginBottom: '20px' }}>
                  <IntegrationCard title="Google Drive" desc="Supplier catalogues (43 PDFs). Delivery note scans. Service account planned." />
                  <IntegrationCard title="Claude API" desc="Haiku model for OCR extraction. ~$8 total across all catalogues." />
                  <IntegrationCard title="QuickBooks" desc="API integration being scoped. Matched line items posted after human approval." />
                  <IntegrationCard title="Google Sheets" desc="Temporary middle layer for selling prices + categories. One-way import into IMS. Being phased out as IMS becomes the SSOT." />
                </div>

                {/* Data sources table from original page */}
                <p style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.08em', color: C.faint, marginBottom: '12px' }}>Data source map</p>
                <div style={{ border: '1px solid #E2E8F0', borderRadius: '8px', overflow: 'hidden', marginBottom: '20px' }}>
                  <div style={{ display: 'grid', gridTemplateColumns: '160px 1fr 130px 110px', background: C.wash, borderBottom: '1px solid #E2E8F0', padding: '8px 12px', gap: '12px' }}>
                    {['Data Point', 'Exact Source', 'How it Enters IMS', 'Location'].map(h => (
                      <span key={h} style={{ fontSize: '10px', fontWeight: 700, color: C.faint, textTransform: 'uppercase' as const, letterSpacing: '0.06em' }}>{h}</span>
                    ))}
                  </div>
                  {[
                    { field: 'SKU ID, Name, Brand', source: 'DATABASE [SSOT] cols A/B/C', how: 'Sync from Sheet', loc: 'Google Sheets', link: SHEET_URL },
                    { field: 'Category', source: 'DATABASE [SSOT] "Item Category" col', how: 'Sync from Sheet', loc: 'Google Sheets', link: SHEET_URL },
                    { field: 'Supplier', source: 'DATABASE [SSOT] "Supplier" col', how: 'Sync from Sheet', loc: 'Google Sheets', link: SHEET_URL },
                    { field: 'Wholesale Cost', source: 'DATABASE [SSOT] "Wholesale Cost (basic)" col', how: 'Sync from Sheet', loc: 'Google Sheets', link: SHEET_URL },
                    { field: 'DaySmart Cost', source: 'DATABASE [SSOT] "Last known cost per DaySmart"', how: 'Sync from Sheet', loc: 'Google Sheets', link: SHEET_URL },
                    { field: 'Selling Price -- Clinic', source: 'DATABASE [SSOT] "Selling Price From DaySmart"', how: 'Sync from Sheet', loc: 'Google Sheets', link: SHEET_URL },
                    { field: 'Selling Price -- Shopify', source: 'DATABASE [SSOT] "Selling Price From Shopify"', how: 'Sync from Sheet', loc: 'Google Sheets', link: SHEET_URL },
                    { field: 'Selling Price -- HKTVMall', source: 'INVENTORY | HKTV tab', how: 'Sync from Sheet', loc: 'Google Sheets', link: HKTV_URL },
                    { field: 'Clinic Stock (qty)', source: 'DaySmart POS manual CSV export', how: 'Stock Import page', loc: 'Clinic (DaySmart)', link: null },
                    { field: 'Warehouse Stock (qty)', source: 'Warehouse manual CSV export', how: 'Stock Import page', loc: 'Warehouse', link: null },
                    { field: 'Wholesale cost (verified)', source: 'Supplier catalogues in Google Drive', how: 'Manual / OCR', loc: 'Google Drive', link: DRIVE_CATS },
                    { field: 'Invoice -- Clinic', source: 'Supplier invoices for clinic orders', how: 'Manual reference', loc: 'Google Drive', link: DRIVE_INV_C },
                    { field: 'Invoice -- Warehouse', source: 'Supplier invoices for warehouse orders', how: 'Manual reference', loc: 'Google Drive', link: DRIVE_INV_W },
                  ].map((row, i, arr) => (
                    <div key={row.field} style={{
                      display: 'grid', gridTemplateColumns: '160px 1fr 130px 110px',
                      gap: '12px', padding: '8px 12px', alignItems: 'start',
                      borderBottom: i < arr.length - 1 ? '1px solid #F1F5F9' : 'none',
                      background: i % 2 === 0 ? 'white' : '#FAFAFA',
                    }}>
                      <span style={{ fontSize: '11px', fontWeight: 600, color: C.ink }}>{row.field}</span>
                      <span style={{ fontSize: '11px', color: C.sub, lineHeight: 1.5 }}>
                        {row.link ? <a href={row.link} target="_blank" rel="noreferrer" style={{ color: C.indigo, fontWeight: 600, textDecoration: 'none', marginRight: '4px' }}>link</a> : null}
                        {row.source}
                      </span>
                      <span style={{ fontSize: '11px', color: C.muted }}>{row.how}</span>
                      <span style={{ fontSize: '11px', color: C.faint }}>{row.loc}</span>
                    </div>
                  ))}
                </div>

                <div style={{ background: C.wash, border: '1px solid #E2E8F0', borderRadius: '8px', textAlign: 'center' as const, padding: '14px' }}>
                  <p style={{ fontSize: '12px', color: C.muted }}>
                    Full architecture deep-dive with current vs target state diagrams &rarr;{' '}
                    <Link to={"/architecture" as never} style={{ fontWeight: 700, color: C.indigo, textDecoration: 'none' }}>Open Architecture Page</Link>
                  </p>
                </div>
              </div>
            )}
          </div>

          {/* ── Footer ────────────────────────────────────────── */}
          <div style={{ textAlign: 'center' as const, fontSize: '11px', color: C.knobOff, padding: '24px 0 0' }}>
            Last updated 26 May 2026 &middot; Edit at <code style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '0.85em', background: C.monoBg, padding: '1px 5px', borderRadius: '3px' }}>frontend/src/app/playbook/page.tsx</code>
          </div>
        </div>
      </div>
  )
}

// ─────────── Subcomponents ──────────────────────────────────────────────────

function Tag({ children, color, bg }: { children: React.ReactNode; color: string; bg: string }) {
  return (
    <span style={{
      display: 'inline-block', fontSize: '10px', fontWeight: 700, padding: '2px 8px',
      borderRadius: '4px', whiteSpace: 'nowrap', color, background: bg,
    }}>
      {children}
    </span>
  )
}

function StatusCard({ status, tag, title, children }: { status: 'live' | 'scoped' | 'planned'; tag: string; title: string; children: React.ReactNode }) {
  const styles = {
    live:    { bg: '#F0FDF4', border: '#22C55E', h4: C.green, p: C.ok, tagBg: C.greenBg, tagColor: C.green },
    scoped:  { bg: '#EFF6FF', border: '#3B82F6', h4: '#1E40AF', p: '#1D4ED8', tagBg: '#DBEAFE', tagColor: '#1E40AF' },
    planned: { bg: C.wash, border: C.knobOff, h4: C.sub, p: C.muted, tagBg: C.monoBg, tagColor: C.muted },
  }[status]

  return (
    <div style={{ padding: '16px 18px', borderRadius: '8px', borderLeft: `4px solid ${styles.border}`, background: styles.bg }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '2px' }}>
        <span style={{ fontSize: '11px', fontWeight: 700, background: styles.tagBg, color: styles.tagColor, padding: '2px 8px', borderRadius: '4px' }}>{tag}</span>
        <h4 style={{ fontSize: '13px', fontWeight: 700, color: styles.h4 }}>{title}</h4>
      </div>
      <p style={{ fontSize: '12px', lineHeight: 1.6, color: styles.p }}>{children}</p>
    </div>
  )
}

function NumberCard({ value, label }: { value: string; label: string }) {
  return (
    <div style={{
      background: C.wash, border: '1px solid #E2E8F0', borderRadius: '8px',
      padding: '16px', textAlign: 'center' as const,
    }}>
      <div style={{ fontSize: '24px', fontWeight: 800, color: C.ink, letterSpacing: '-1px' }}>{value}</div>
      <div style={{ fontSize: '11px', color: C.muted, marginTop: '4px' }}>{label}</div>
    </div>
  )
}

function FlowBox({ variant, children }: { variant: 'old' | 'new'; children: React.ReactNode }) {
  const s = variant === 'old'
    ? { background: C.badBg, color: C.redInk, border: '1px solid #FECACA', textDecoration: 'line-through' as const, opacity: 0.7 }
    : { background: '#F0FDF4', color: C.green, border: '1px solid #BBF7D0', textDecoration: 'none' as const, opacity: 1 }
  return (
    <div style={{
      padding: '8px 14px', borderRadius: '6px', fontSize: '12px', fontWeight: 600,
      textAlign: 'center' as const, flex: 1, ...s,
    }}>
      {children}
    </div>
  )
}

function FlowArrow({ variant }: { variant: 'old' | 'new' }) {
  return (
    <div style={{ textAlign: 'center' as const, color: variant === 'old' ? '#FECACA' : '#BBF7D0', fontSize: '14px' }}>&darr;</div>
  )
}

function StepList({ children }: { children: React.ReactNode }) {
  return <ol style={{ listStyle: 'none', padding: 0, counterReset: 'steps', margin: 0 }}>{children}</ol>
}

function StepItem({ children }: { children: React.ReactNode }) {
  return (
    <li style={{
      display: 'flex', gap: '12px', alignItems: 'flex-start',
      padding: '10px 0', borderBottom: '1px solid #F1F5F9',
      fontSize: '12.5px', color: C.sub, lineHeight: 1.6,
      counterIncrement: 'steps',
    }}>
      <span style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        width: '24px', height: '24px', minWidth: '24px', borderRadius: '50%',
        background: C.primaryBg, color: C.indigoInk, fontSize: '11px', fontWeight: 700,
        flexShrink: 0, marginTop: '1px',
      }}>
        {/* Counter handled via CSS; fallback handled by list order */}
      </span>
      <div>{children}</div>
    </li>
  )
}

function ConfTier({ rank, bg, border, rankBg, label, desc, labelColor, descColor }: {
  rank: number; bg: string; border: string; rankBg: string; label: string; desc: string; labelColor: string; descColor: string
}) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: '12px',
      padding: '10px 14px', borderRadius: '6px', marginBottom: '6px',
      background: bg, border: `1px solid ${border}`,
    }}>
      <div style={{
        width: '28px', height: '28px', borderRadius: '50%',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: '12px', fontWeight: 700, color: 'white', flexShrink: 0, background: rankBg,
      }}>
        {rank}
      </div>
      <div>
        <div style={{ fontSize: '13px', fontWeight: 600, color: labelColor }}>{label}</div>
        <div style={{ fontSize: '11px', color: descColor, marginTop: '1px' }}>{desc}</div>
      </div>
    </div>
  )
}

function ArchLayer({ bg, border, labelColor, label, children }: {
  bg: string; border: string; labelColor: string; label: string; children: React.ReactNode
}) {
  return (
    <div style={{ padding: '14px 16px', borderRadius: '8px', marginBottom: '8px', background: bg, border: `1px solid ${border}` }}>
      <div style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase' as const, letterSpacing: '0.06em', marginBottom: '6px', color: labelColor }}>{label}</div>
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>{children}</div>
    </div>
  )
}

function ArchBox({ border, color, children }: { border: string; color: string; children: React.ReactNode }) {
  return (
    <div style={{
      background: 'white', border: `1px solid ${border}`, borderRadius: '6px',
      padding: '8px 12px', fontSize: '11px', fontWeight: 600,
      flex: 1, minWidth: '120px', textAlign: 'center' as const, color,
    }}>
      {children}
    </div>
  )
}

function EndpointGrid({ children }: { children: React.ReactNode }) {
  return <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>{children}</div>
}

function EndpointRow({ method, path, desc }: { method: string; path: string; desc: string }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '140px 1fr', gap: '12px', fontSize: '11px' }}>
      <dt style={{ fontWeight: 700, color: C.sub, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
        {method ? `${method} ` : ''}{path}
      </dt>
      <dd style={{ color: C.muted, margin: 0 }}>{desc}</dd>
    </div>
  )
}

function IntegrationCard({ title, desc }: { title: string; desc: string }) {
  return (
    <div style={{ background: 'white', border: '1px solid #E2E8F0', borderRadius: '8px', textAlign: 'center' as const, padding: '14px' }}>
      <p style={{ fontSize: '12px', fontWeight: 700, color: C.ink, marginBottom: '4px' }}>{title}</p>
      <p style={{ fontSize: '11px', color: C.muted }}>{desc}</p>
    </div>
  )
}
