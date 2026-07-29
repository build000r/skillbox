# Oracle broker caller policy

The Oracle broker authenticates its transport peer first, then gives the
resulting caller ID to `runtime_manager.oracle_policy`. Request bodies cannot
choose or override that ID.

Policy admission happens before attachment staging or browser contact. The
policy API accepts only a strict facts record:

- mode (`standard` or `deep-research`);
- UTF-8 prompt byte count;
- attachment count and validated aggregate bytes;
- requested runtime timeout.

Prompt text, file paths, hooks, environment values, executable paths,
`browserConfig`, CDP targets, cookies, profiles, and tokens are not fields in
this contract. Unknown fields fail closed. Admission requires the exact
`OracleRequestFacts` type, revalidates every primitive field, and locally
recomputes prompt-plus-attachment bytes. Subclasses, mutated instances, and an
overridden aggregate property cannot weaken the limits.

## Configuration

The broker will supply a mapping with this shape:

```json
{
  "schema": "skillbox.oracle-policy.v1",
  "callers": {
    "local": {
      "modes": ["standard", "deep-research"],
      "max_prompt_bytes": 262144,
      "max_files": 8,
      "max_attachment_bytes": 52428800,
      "max_request_bytes": 52690944,
      "max_concurrent": 2,
      "max_requests_per_window": 30,
      "max_bytes_per_window": 268435456,
      "window_seconds": 3600,
      "max_runtime_seconds": 7200,
      "lease_grace_seconds": 60
    }
  }
}
```

There is no wildcard or implicit default caller. Each authenticated Tailnet or
SSH identity needs its own explicit entry. Mode, prompt, file, byte, runtime,
concurrency, rolling request-count, and rolling byte limits are all mandatory.

## One-time authority enrollment

Policy state is usable only after the trusted local Skillbox service installer
explicitly enrolls a separate authority directory:

```python
provision_oracle_policy_authority(
    policy,
    "/var/lib/skillbox/oracle-policy",
    authority_directory="/var/lib/skillbox-authority/oracle-policy",
)

engine = OraclePolicyEngine(
    policy,
    "/var/lib/skillbox/oracle-policy",
    authority_directory="/var/lib/skillbox-authority/oracle-policy",
)
```

`provision_oracle_policy_authority()` is a narrow first-install operation. It
creates and fsyncs the private state directory, namespace witness, and authority
records, and refuses an existing enrollment. It is service-owner tooling, not a
broker RPC, request option, recovery path, or remote caller capability.

Normal `OraclePolicyEngine` construction is read-only with respect to
enrollment. A missing authority directory, manifest, history, head, state
directory, anchor, or namespace witness is a denial; construction never treats
missing records as a virgin quota generation and never creates replacements.
The authority path must be canonical, absolute, and lexically outside the state
directory and namespace anchor. Operators should place it in a separately
protected service-owned location.

Authority and namespace generations come only from validated operating-system
CSPRNG output. Entropy exceptions or malformed output are converted to stable,
label-only `OraclePolicyError` codes. Because enrollment creates its fixed
identities before generating those values, an entropy failure may leave a
partial private bootstrap. That bootstrap is permanently unusable, cannot be
resumed or reenrolled in place, and requires deliberate service-owner cleanup.

## Reservation lifecycle

`OraclePolicyEngine.reserve()` serializes admission on the pre-enrolled
authority directory, validates the authority journal head, namespace witness,
and state head while that stable lock is held, accounts the request immediately,
and returns an opaque reservation. State operations stay anchored to opened
directory descriptors through the decision. The browser-facing broker runs
only inside `OraclePolicyEngine.admission()`, which reserves before yielding and
releases concurrency in `finally`.

Request and byte quota events remain for the rolling window after release.
Dead workers recover when their bounded runtime plus lease grace expires.
Backward wall-clock movement, corrupt state, duplicate JSON keys, unsafe
permissions, symlinks, hardlinks, directory replacement, and lock timeout all
fail closed. Persisted JSON must also equal the canonical, key-sorted,
separator-minimized ASCII encoding followed by exactly one newline; semantically
equivalent pretty-printed state is rejected.

State and authority paths must be exact canonical absolute strings. Canonicality
is checked on the raw `os.fspath()` result before constructing `Path`; bytes,
relative paths, filesystem root, `/./`, `..` components, duplicate separators,
trailing separators, NULs, and unpaired Unicode surrogates are rejected without
echoing the input. Every component is then traversed from the filesystem root
with descriptor-relative opens and `O_NOFOLLOW`; intermediate and final
symlinks are rejected. Close, entropy, lock-clock, and other operating-system
boundary failures are normalized to the same non-sensitive
`OraclePolicyError` surface. The immediate parent must be owned by the operator
and not group- or world-writable. Only the explicit enrollment operation may
create missing components, one at a time as private directories instead of
using recursive, path-following creation.

State lives in an operator-owned `0700` directory with a `0600`, single-link
regular state file. Its sibling `.oracle-policy-namespaces/` anchor is also
operator-owned `0700`. A private `0600`, single-link witness in that anchor
binds the canonical state path, authority generation and sequence,
parent/anchor/state device and inode identities, a random namespace generation,
and the SHA-256 fingerprint of the entire normalized policy. The same namespace
generation and policy fingerprint are required in the state document.

The separate authority directory is operator-owned `0700`. Its fixed-inode,
`0600`, single-link files are:

- an immutable enrollment manifest binding the canonical authority and state
  paths, policy fingerprint, authority and namespace generations, and original
  authority-parent/authority/manifest/head/history/state-parent/anchor/state/
  namespace device and inode identities;
- an append-only canonical JSON-lines history whose contiguous entries include
  the prior-entry hash, sequence, pending or committed state head, and exact
  namespace-witness digest;
- a fixed-inode head record containing the current sequence, entry hash, and
  exact history byte length.

Every read validates the complete hash chain, exact canonical bytes, head and
history agreement, bound inode identities, namespace digest, and state digest.
The running engine also retains its last observed authority sequence and hash,
so a valid-prefix truncation cannot move that process backward. A fresh engine
given a coherently older history and head still rejects the newer live
namespace/state pair. Restoring authority files by ordinary rename, replacement,
or copy-tree backup replacement changes their enrolled inodes and is rejected.

Every state change is published under the authority lock in five durable
phases:

1. append and fsync an authority `pending` entry, then fsync its head;
2. rewrite and fsync the fixed-inode namespace witness with that pending head;
3. atomically write and fsync the exact canonical state bytes;
4. rewrite and fsync the namespace witness with the next committed sequence;
5. append and fsync the matching authority `committed` entry, then fsync its
   head.

Initial state may be absent only at the enrolled sequence-zero committed head.
Any partial journal append, journal/head disagreement, or visible pending
authority entry means publication was interrupted and fails closed permanently.
Policy admission never rolls it backward, restores a prior pair, guesses which
side to trust, or completes it later. Coherent state/witness rewriting at the
same revision, rollback to an older committed pair, restoration after a pending
crash, replacement of both state and namespace directories, authority record
loss/corruption/prefix rollback, and authority identity replacement are denied
before browser contact.

Reservation IDs and atomic-state temporary names also come only from validated
CSPRNG output. Reservation entropy failure occurs before authority publication
and leaves all durable bytes unchanged. Temporary-name entropy failure occurs
after the authority and witness pending records are durable, so it returns the
stable state-write denial while deliberately leaving that pending transition
fail-closed. Retry entropy failures receive the same label-only reservation
denial; exception text is never exposed and browser callbacks remain unentered.

## Honest local authority boundary

Filesystem records cannot protect against the same trusted local service owner
who can rewrite the policy code or deliberately restore old bytes in place
across every fixed-inode authority record plus the matching state/witness pair.
Deliberately destroying all three domains and invoking enrollment again is
likewise an owner-authorized reset. Those actions define the local authority
boundary; the implementation does not claim to prevent them.

Routine broker code and remote callers receive no reset, rollback, repair, or
enrollment surface. A policy change or intentional reset therefore requires the
service owner to stop and drain the broker, deliberately remove the old
enrollment domains, and run the separate provisioning operation again.

State contains caller IDs, mode, timestamps, opaque reservation IDs, byte
counts, the monotonic revision, and the opaque namespace generation only.
Witness and authority records contain only paths, filesystem identities,
opaque generations, policy/state/witness digests, sequences, phases, and
committed/pending heads. They store no prompts, attachment paths, browser state,
authentication material, or secrets.
