# Discovery, group preference scope and quiet rechecks

The four October 4 audit findings are addressed together:

1. Ordinary unread discoveries are durable observations. The initial page is
   recorded before offscreen navigation, and each searched page records both
   unread and mention discoveries. A new unread conversation stops the search
   and is handled on its current frame. Stored discoveries contain names, not
   reusable click coordinates or reply authorization. Excluded/system chats and
   quiet patrol-only rows are not retained. Mentions retain highest priority;
   unread and active work precede quiet patrols.
2. `ScannedRows` carries the successful list-recognition frame independently of
   list length. A fully validated empty result can initiate offscreen search.
   Failure, timeout, malformed row mappings and non-list results cannot. Cache
   copies preserve this evidence; it cannot authorize a different frame. Existing
   fresh chrome, row-position and conversation-identity checks still apply.
3. Observed scalar style corrections only use the group-scoped candidate path.
   The previously supported phrase “机器人不要表情” is recognized there. Unknown
   scalar phrases cannot fall through to global preferences. Legacy global
   observed corrections are suppressed immediately and disabled on summarization.
   Observed avoided words join their live source and conversation when loaded;
   revocation is immediate. Explicit manual global preferences remain global.
   Aggregate expression statistics remain weak global preferences as before.
4. A confirmed no-reply inspection starts a 120-second quiet interval. Further
   confirmed unchanged inspections grow it to 240 then at most 300 seconds.
   Skips never renew the interval. Changed pixels, unread/mention evidence,
   pending work, failures and unknown fingerprints bypass/reset quiet state.
   Restart starts without quiet cache. No send/identity authorization is cached.

Validation: the upgrade gate includes 16 new isolated regressions for discovery
retention/restart, empty-list evidence, scope migration, source revocation and
adaptive scheduling. It also runs all existing suites and 35 sanitized history
replay checks. No test sends messages to a real group. Runtime latency improvements
require new production samples and are not inferred from passing offline tests.
