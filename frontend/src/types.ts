export type Location = 'ON_ICE' | 'BENCH' | 'PENALTY_BOX'
export type Position = 'C' | 'LW' | 'RW' | 'D' | 'G'
export type Player = { id: string; team_id: string; name: string; number: string | number; position: Position; assigned_role?: string; eligible: boolean; location: Location; total_toi_ms: number; current_shift_ms: number; completed_shifts: number; average_shift_ms: number; longest_shift_ms: number; toi_by_period: Record<string, number>; strength_toi: unknown }
export type Team = { id: string; name: string; short_name: string; color: string; side: 'home' | 'away' }
export type Line = { id: string; team_id: string; name: string; kind: 'FORWARD' | 'DEFENCE' | 'SPECIAL'; player_ids: string[] }
export type MatchEvent = { id: string; type: string; payload: Record<string, unknown>; playing_ms: number; period: number; remaining_ms: number; actor: string; voided: boolean; created_at?: string }
export type Shift = { player_id: string; start_ms: number; end_ms: number | null; period: number; [key: string]: unknown }
export type Snapshot = { game: { id: string; name: string; status: string; competition: string; scheduled_start?: string; period_lengths_ms: number[]; external_status?: string; stadium?: string; coverage?: { period: number; remaining_ms: number; partial_match?: boolean } }; teams: Team[]; players: Player[]; lines: Line[]; clock: { period: number; remaining_ms: number; running: boolean; playing_ms: number; anchor_unix_ms: number | null; period_playing_ms: Record<string, number> }; events: MatchEvent[]; shifts: Shift[]; version: number; server_time_ms?: number; integration?: { provider: string; external_game_id?: string; imported_at_ms?: number; last_refreshed_at_ms?: number; [key: string]: unknown }; coverage?: { period: number; remaining_ms: number; [key: string]: unknown }; warnings: string[]; strengths: Record<string, unknown> }
export type Suggestion = { player_id: string; score: number; reasons: string[]; eligible: boolean; observations: number | unknown[]; confidence?: string }
export type SuggestionGroup = { player_ids: string[]; score: number; reasons: string[]; name?: string }
export type Suggestions = { players: Suggestion[]; groups: SuggestionGroup[]; combinations: SuggestionGroup[] }
export type GameSummary = { id: string; name: string; status: string; [key: string]: unknown }
export type Action = (type: string, payload: Record<string, unknown>) => Promise<boolean>
