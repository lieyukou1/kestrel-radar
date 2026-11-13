export type TaxonomyItem = {
  name: string;
  description: string;
};

export type DomainSource = {
  id: string;
  name: string;
  kind: string;
  authority: string;
  enabled: boolean;
};

export type DomainDigestRef = {
  date: string;
  kind: string;
  status: string;
};

export type DomainSummary = {
  id: string;
  name: string;
  taxonomy: TaxonomyItem[];
  defaults: Record<string, string>;
  sources: DomainSource[];
  weights: Record<string, unknown>;
  db: string;
  db_exists: boolean;
  last_digest: DomainDigestRef | null;
};

export type DigestSummary = {
  date: string;
  kind: string;
  status: string;
  created_at: string;
  sent_at: string | null;
  count: number | null;
  events_count: number | null;
  theme: string;
  stats: Record<string, unknown>;
};

export type Article = {
  id: number;
  kind: string;
  title: string;
  abstract: string | null;
  url: string;
  published_at: string | null;
  first_seen_at: string;
  authority: string;
  status: string;
  tag: string | null;
  tags: string[];
  read: boolean;
  read_at: string | null;
  read_count: number;
  local_path: string | null;
};

export type SummaryPayload = Record<string, unknown>;

export type ReadingEvidence = {
  id: number;
  kind: string | null;
  quote: string | null;
  anchor: string | null;
  metric: string | null;
  value: string | null;
  unit: string | null;
  baseline: string | null;
  condition: string | null;
  sample: string | null;
};

export type ReadingClaim = {
  id: number;
  kind: string;
  text: string;
  claim_type: string | null;
  confidence: string | null;
  quote: string | null;
  anchor: string | null;
  question: string | null;
  status: string;
  section_id: number | null;
  evidence: ReadingEvidence[];
};

export type ReadingSection = {
  id: number;
  ord: number;
  heading: string | null;
  role: string | null;
  summary: string | null;
  anchor: string | null;
};

export type ReadingPayload = {
  thesis?: string;
  problem?: string;
  method?: string;
  article_type?: string;
  audience?: string;
  outline?: { heading: string; role: string; summary: string; anchor: string }[];
  parts?: {
    index: number;
    total: number;
    headings: string;
    summary: string;
    key_claims?: number[];
    open_questions?: string[];
  }[];
  coverage?: { chunks_total: number; chunks_read: number; chunks_skipped?: number };
  questions?: string[];
  key_terms?: { term: string; definition: string }[];
  claims?: (ReadingClaim & { local_id?: number })[];
  main_claims?: {
    claim_id: number;
    local_id?: number;
    text: string;
    supports?: number[];
    note?: string;
  }[];
  relations?: {
    from_claim_id: number;
    to_claim_id: number;
    relation: string;
    note: string;
  }[];
  narrative?: string;
  critique?: {
    unverified?: string[];
    weak_evidence?: string[];
    counterpoints?: string[];
    open_questions?: string[];
  };
  limitations?: string[];
  evidence?: string[];
  l3?: string;
  community?: Record<string, string[]>;
};

export type EventItem = Article & {
  angle: string | null;
  item_rank: number;
  summary: SummaryPayload | null;
  saved: boolean;
};

export type EventTopic = {
  id: number;
  label: string;
  summary: string;
  entity: string;
  dimension: string | null;
  score: number;
  item_count: number;
  event_date: string | null;
  event_key: string | null;
  first_date: string | null;
  day_index: number;
  new_count: number;
  items: EventItem[];
};

export type DigestDetail = {
  digest: DigestSummary;
  events: EventTopic[];
  briefs: (Article & { section: string; reason: string | null })[];
};

export type ArticleDetail = {
  document: Article;
  summary: SummaryPayload | null;
  kb: SummaryPayload | null;
  reading: ReadingPayload | null;
  sections: ReadingSection[];
  claims: ReadingClaim[];
  notes: { content: string; created_at: string }[];
  sources: { source_id: string; name: string; kind: string }[];
  appearances: {
    digest_date: string;
    kind: string;
    label: string | null;
    section: string;
  }[];
  saved: boolean;
  read: boolean;
};

export type ArchiveResult = {
  total: number;
  limit: number;
  offset: number;
  items: Article[];
};

export type KBItem = Article & {
  deep_read_at: string | null;
  saved_at: string | null;
  deep_read: number;
};

export type KBResult = {
  counts: { deep: number; only_saved: number; total: number };
  facets: { dimensions: string[]; kinds: string[] };
  items: KBItem[];
};

export type SearchHit = {
  document_id: number;
  title: string;
  url: string;
  tag: string;
  kind: string;
  score: number;
  reason: string;
  published_at: string | null;
  pushed_recently: boolean;
  discussion_url: string | null;
  in_kb: boolean;
  saved: boolean;
};

export type ExternalHit = {
  source: string;
  title: string;
  url: string;
  meta: string;
};

export type SearchResult = {
  query: string;
  local: SearchHit[];
  external: ExternalHit[];
  elapsed_ms: number;
  note: string;
};

export type QACitation = {
  label: string;
  unit_type: string;
  document_id: number;
  claim_id: number | null;
  evidence_id: number | null;
  anchor: string;
  quote: string;
  score: number;
};

export type QAResult = {
  supported: boolean;
  answer?: string;
  sources?: { document_id: number; title: string; url: string; anchor?: string }[];
  citations?: QACitation[];
  related?: SearchHit[];
};

export type SettingsPayload = {
  domain: DomainSummary | null;
  paused_dimensions: string[];
  paused_sources: string[];
  preferences: Record<string, number>;
  theme: Record<string, unknown> | null;
};

export type RunRecord = {
  id: number;
  job: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  error: string | null;
  stats: Record<string, unknown>;
};

export type SourceWindow = {
  source_id: string;
  kind: string;
  name: string;
  enabled: boolean;
  last_fetched_at: string | null;
  items_in_window: number;
};

export type StatusPayload = {
  documents_by_status: Record<string, number>;
  today_digest: DigestSummary | null;
  today_llm: { calls: number; cost_cny: number };
  recent_runs: RunRecord[];
  last_7_days: {
    window_days: number;
    llm_total_cny: number;
    llm_calls: number;
    sources: SourceWindow[];
    llm_by_stage: { stage: string; calls: number; cost_cny: number }[];
    digests: {
      date: string;
      kind: string;
      status: string;
      sent_at: string | null;
      count: number | null;
    }[];
    failed_runs: {
      job: string;
      started_at: string;
      status: string;
      error: string | null;
    }[];
  };
};
