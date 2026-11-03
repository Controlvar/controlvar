/**
 * Segments API client (#440): named audiences that flag and experiment
 * targeting rules can name with `in_segment` / `not_in_segment`.
 *
 * Segments use the EXPERIMENT permissions: every role can read them; ADMIN
 * and DEVELOPER, or a custom role or grant, change them. Collection URLs
 * have no trailing slash
 * (`/api/v1/segments`); with one the API answers 307.
 */
import { apiFetch } from '@/services/api';

export type SegmentKind = 'rules' | 'id_list';
export type SegmentStatus = 'active' | 'inactive' | 'archived';

export interface Segment {
  id: string;
  name: string;
  description?: string | null;
  kind: SegmentKind;
  /** The dashboard rule shape; null for an id list. Stored rules saved before #440 may be any JSON. */
  rules: unknown;
  status: SegmentStatus;
  created_at: string;
  updated_at: string;
  /** Members of an id list, on `GET /segments/{id}` only. */
  member_count?: number | null;
}

export interface SegmentUsage {
  segment_id: string;
  experiments: Array<{ id: string; name: string; status?: string | null }>;
  feature_flags: Array<{ id: string; name: string }>;
}

export interface SegmentMembership {
  segment_id: string;
  segment_name: string;
  is_member: boolean;
}

export interface AudiencePreview {
  estimated_percentage: number;
  sample_size: number;
  matched: number;
}

export interface MembersAdded {
  added: number;
  already_members: number;
  member_count: number;
}

export interface MembersRemoved {
  removed: number;
  not_members: number;
  member_count: number;
}

export interface CreateSegmentRequest {
  name: string;
  description?: string;
  kind: SegmentKind;
  rules?: Record<string, unknown>;
}

/** The API's page size cap for the list. */
export const SEGMENT_LIST_MAX = 200;

/** Rows per page on the Segments page. */
export const SEGMENT_PAGE_SIZE = 50;

export const SegmentsService = {
  /** One page of segments; no `status` means every status. */
  list(params: { status?: SegmentStatus; limit: number; offset: number }): Promise<Segment[]> {
    return apiFetch<Segment[]>('/api/v1/segments', {
      query: { status: params.status, limit: params.limit, offset: params.offset },
    });
  },

  /**
   * Every segment, any status (the picker shows the names of inactive and
   * archived segments that stored rules still name). Pages of 200; stops at
   * the first short page.
   */
  async listAll(): Promise<Segment[]> {
    const all: Segment[] = [];
    for (let page = 0; page < 500; page += 1) {
      const items = await SegmentsService.list({
        limit: SEGMENT_LIST_MAX,
        offset: page * SEGMENT_LIST_MAX,
      });
      all.push(...items);
      if (items.length < SEGMENT_LIST_MAX) break;
    }
    return all;
  },

  get(id: string): Promise<Segment> {
    return apiFetch<Segment>(`/api/v1/segments/${encodeURIComponent(id)}`);
  },

  create(body: CreateSegmentRequest): Promise<Segment> {
    return apiFetch<Segment>('/api/v1/segments', { method: 'POST', json: body });
  },

  updateRules(id: string, rules: Record<string, unknown>): Promise<Segment> {
    return apiFetch<Segment>(`/api/v1/segments/${encodeURIComponent(id)}`, {
      method: 'PUT',
      json: { rules },
    });
  },

  archive(id: string): Promise<void> {
    return apiFetch<void>(`/api/v1/segments/${encodeURIComponent(id)}`, { method: 'DELETE' });
  },

  usage(id: string): Promise<SegmentUsage> {
    return apiFetch<SegmentUsage>(`/api/v1/segments/${encodeURIComponent(id)}/experiments`);
  },

  /** Whether `userId` is in an id-list segment (the server reads `user_context.user_id`). */
  checkUser(id: string, userId: string): Promise<SegmentMembership> {
    return apiFetch<SegmentMembership>(`/api/v1/segments/${encodeURIComponent(id)}/evaluate`, {
      method: 'POST',
      json: { user_context: { user_id: userId } },
    });
  },

  /** Estimate how many recently assigned users `rules` match. */
  preview(id: string, name: string, rules: Record<string, unknown>): Promise<AudiencePreview> {
    return apiFetch<AudiencePreview>(`/api/v1/segments/${encodeURIComponent(id)}/preview`, {
      method: 'POST',
      json: { name, kind: 'rules', rules },
    });
  },

  addMembers(id: string, ids: string[]): Promise<MembersAdded> {
    return apiFetch<MembersAdded>(`/api/v1/segments/${encodeURIComponent(id)}/members`, {
      method: 'POST',
      json: { add: ids },
    });
  },

  removeMembers(id: string, ids: string[]): Promise<MembersRemoved> {
    return apiFetch<MembersRemoved>(`/api/v1/segments/${encodeURIComponent(id)}/members/remove`, {
      method: 'POST',
      json: { remove: ids },
    });
  },
};
