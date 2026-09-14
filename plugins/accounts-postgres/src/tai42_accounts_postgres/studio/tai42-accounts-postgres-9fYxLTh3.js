import { jsxs as h, jsx as e } from "react/jsx-runtime";
import { useMemo as S, useState as o, useCallback as k, useEffect as $ } from "react";
import { useAuth as x, useOnUnauthorized as B, errorMessage as N, Button as b, Spinner as U, ErrorState as w, EmptyState as V, Table as j, THead as H, TR as I, TH as C, TBody as M, TD as R, Badge as E, CopyField as q, Dialog as D, Field as T, TextInput as z, Select as A, ConfirmDialog as J } from "@tai42/studio-sdk";
async function F(n, i, r, t) {
  const l = new Headers({ accept: "application/json" });
  i !== null && l.set("x-api-key", i), t?.body !== void 0 && l.set("content-type", "application/json");
  const s = await fetch(n, {
    method: t?.method ?? "GET",
    headers: l,
    body: t?.body !== void 0 ? JSON.stringify(t.body) : void 0,
    signal: t?.signal
  });
  if (s.status === 401)
    throw r(), new Error("Your session has expired — sign in again.");
  const a = await s.text(), u = a === "" ? {} : JSON.parse(a);
  if (!s.ok) {
    const d = typeof u.error == "string" && u.error.length > 0 ? u.error : `Request failed (${String(s.status)})`;
    throw new Error(d);
  }
  return u.data;
}
function G() {
  const { token: n } = x(), i = B();
  return S(() => {
    const r = (t, l) => F(t, n, i, l);
    return {
      listUsers: (t) => r("/api/auth/users", { signal: t }).then((l) => l.users),
      listRoles: (t) => r("/api/auth/roles", { signal: t }),
      createUser: (t) => r("/api/auth/users", { method: "POST", body: t }),
      setRole: (t, l) => r(`/api/auth/users/${encodeURIComponent(t)}`, {
        method: "PUT",
        body: { role: l }
      }).then(() => {
      }),
      setDisabled: (t, l) => r(`/api/auth/users/${encodeURIComponent(t)}`, {
        method: "PUT",
        body: { disabled: l }
      }).then(() => {
      }),
      deleteUser: (t) => r(`/api/auth/users/${encodeURIComponent(t)}`, {
        method: "DELETE"
      }).then(() => {
      }),
      regenerateInvite: (t) => r(`/api/auth/users/${encodeURIComponent(t)}/invite`, {
        method: "POST"
      })
    };
  }, [n, i]);
}
function W() {
  const n = G(), [i, r] = o(null), [t, l] = o([]), [s, a] = o(null), [u, d] = o(!0), [m, p] = o(0), c = k(() => {
    p((g) => g + 1);
  }, []);
  return $(() => {
    const g = new AbortController();
    return d(!0), a(null), Promise.all([n.listUsers(g.signal), n.listRoles(g.signal)]).then(
      ([y, v]) => {
        g.signal.aborted || (r(y), l(v), d(!1));
      },
      (y) => {
        g.signal.aborted || (a(N(y)), d(!1));
      }
    ), () => {
      g.abort();
    };
  }, [n, m]), { api: n, users: i, roles: t, loadError: s, loading: u, reload: c };
}
function Y({
  user: n,
  onAction: i
}) {
  return /* @__PURE__ */ h("div", { className: "users-row-actions", children: [
    /* @__PURE__ */ e(b, { type: "button", onClick: () => i({ kind: "role", user: n }), children: "Change role" }),
    /* @__PURE__ */ e(b, { type: "button", onClick: () => i({ kind: "disable", user: n }), children: n.disabled ? "Enable" : "Disable" }),
    n.pending_invite ? /* @__PURE__ */ e(b, { type: "button", onClick: () => i({ kind: "invite", user: n }), children: "Regenerate invite" }) : null,
    /* @__PURE__ */ e(b, { type: "button", variant: "danger", onClick: () => i({ kind: "delete", user: n }), children: "Delete" })
  ] });
}
function K({ user: n }) {
  return n.pending_invite ? /* @__PURE__ */ e(E, { variant: "warning", children: "Invite pending" }) : n.disabled ? /* @__PURE__ */ e(E, { variant: "danger", children: "Disabled" }) : /* @__PURE__ */ e(E, { variant: "success", children: "Active" });
}
function Q(n) {
  const i = new Date(n);
  return Number.isNaN(i.getTime()) ? n : i.toLocaleDateString();
}
function X({
  loading: n,
  users: i,
  loadError: r,
  reload: t,
  onAction: l
}) {
  return n && i === null ? /* @__PURE__ */ e(U, { label: "Loading users" }) : r !== null && i === null ? /* @__PURE__ */ e(w, { message: r, onRetry: t }) : i !== null && i.length === 0 ? /* @__PURE__ */ e(
    V,
    {
      title: "No users yet",
      description: "Invite the first user to get them a one-time sign-in link."
    }
  ) : /* @__PURE__ */ h(j, { children: [
    /* @__PURE__ */ e(H, { children: /* @__PURE__ */ h(I, { children: [
      /* @__PURE__ */ e(C, { children: "Email" }),
      /* @__PURE__ */ e(C, { children: "Role" }),
      /* @__PURE__ */ e(C, { children: "Status" }),
      /* @__PURE__ */ e(C, { children: "Created" }),
      /* @__PURE__ */ e(C, { children: /* @__PURE__ */ e("span", { className: "users-cell-muted", children: "Actions" }) })
    ] }) }),
    /* @__PURE__ */ e(M, { children: (i ?? []).map((s) => /* @__PURE__ */ h(I, { children: [
      /* @__PURE__ */ e(R, { children: s.email }),
      /* @__PURE__ */ e(R, { children: /* @__PURE__ */ e(E, { variant: "primary", children: s.role }) }),
      /* @__PURE__ */ e(R, { children: /* @__PURE__ */ e(K, { user: s }) }),
      /* @__PURE__ */ e(R, { children: /* @__PURE__ */ e("span", { className: "users-cell-muted", children: Q(s.created_at) }) }),
      /* @__PURE__ */ e(R, { children: /* @__PURE__ */ e(Y, { user: s, onAction: l }) })
    ] }, s.user_id)) })
  ] });
}
function P({ result: n }) {
  return /* @__PURE__ */ e("div", { className: "users-dialog-body", children: /* @__PURE__ */ e(
    q,
    {
      label: "Invite link",
      value: n.login_path,
      caption: "Copy this link now — it is shown only once. Send it to the user; opening it lets them set a password and sign in."
    }
  ) });
}
function Z({
  roles: n,
  api: i,
  onClose: r,
  onCreated: t
}) {
  const [l, s] = o(""), [a, u] = o(n[0]?.name ?? ""), [d, m] = o(!1), [p, c] = o(null), [g, y] = o(null), v = S(() => n.map((f) => ({ value: f.name, label: f.name })), [n]), O = l.trim().length > 0 && a.length > 0 && !d, L = k(() => {
    m(!0), c(null), i.createUser({ email: l.trim(), role: a }).then(
      (f) => {
        y(f), m(!1), t();
      },
      (f) => {
        c(N(f)), m(!1);
      }
    );
  }, [i, l, a, t]);
  return g !== null ? /* @__PURE__ */ h(
    D,
    {
      title: "Invite created",
      open: !0,
      onOpenChange: (f) => {
        f || r();
      },
      children: [
        /* @__PURE__ */ e(P, { result: g }),
        /* @__PURE__ */ e("div", { className: "users-dialog-actions", style: { marginTop: "var(--tai-space-4)" }, children: /* @__PURE__ */ e(b, { type: "button", variant: "primary", onClick: r, children: "Done" }) })
      ]
    }
  ) : /* @__PURE__ */ e(
    D,
    {
      title: "Invite user",
      open: !0,
      onOpenChange: (f) => {
        f || r();
      },
      children: /* @__PURE__ */ h("div", { className: "users-dialog-body", children: [
        /* @__PURE__ */ e(T, { label: "Email", children: /* @__PURE__ */ e(
          z,
          {
            type: "email",
            "aria-label": "Email",
            value: l,
            onChange: (f) => {
              s(f.target.value);
            }
          }
        ) }),
        /* @__PURE__ */ e(T, { label: "Role", children: /* @__PURE__ */ e(
          A,
          {
            "aria-label": "Role",
            options: v,
            value: a,
            onValueChange: u,
            placeholder: v.length === 0 ? "No roles available" : "Select a role",
            disabled: v.length === 0
          }
        ) }),
        p !== null ? /* @__PURE__ */ e(w, { message: p }) : null,
        /* @__PURE__ */ h("div", { className: "users-dialog-actions", children: [
          /* @__PURE__ */ e(b, { type: "button", onClick: r, children: "Cancel" }),
          /* @__PURE__ */ h(b, { type: "button", variant: "primary", disabled: !O, onClick: L, children: [
            d ? /* @__PURE__ */ e(U, { label: "Creating invite" }) : null,
            "Send invite"
          ] })
        ] })
      ] })
    }
  );
}
function _({
  title: n,
  confirmLabel: i,
  pendingLabel: r,
  confirmVariant: t,
  run: l,
  onClose: s,
  onDone: a,
  children: u
}) {
  const [d, m] = o(!1), [p, c] = o(null), g = k(() => {
    m(!0), c(null), l().then(
      () => {
        a();
      },
      (y) => {
        c(y instanceof Error ? y : new Error(String(y))), m(!1);
      }
    );
  }, [l, a]);
  return /* @__PURE__ */ e(
    J,
    {
      title: n,
      confirmLabel: i,
      pendingLabel: r,
      confirmVariant: t,
      isPending: d,
      error: p,
      onConfirm: g,
      onClose: s,
      children: u
    }
  );
}
function ee({
  user: n,
  roles: i,
  api: r,
  onClose: t,
  onDone: l
}) {
  const [s, a] = o(n.role), [u, d] = o(!1), [m, p] = o(null), c = S(() => i.map((v) => ({ value: v.name, label: v.name })), [i]), g = s !== n.role && !u, y = k(() => {
    d(!0), p(null), r.setRole(n.user_id, s).then(
      () => {
        l();
      },
      (v) => {
        p(N(v)), d(!1);
      }
    );
  }, [r, n.user_id, s, l]);
  return /* @__PURE__ */ e(
    D,
    {
      title: `Change role — ${n.email}`,
      open: !0,
      onOpenChange: (v) => {
        v || t();
      },
      children: /* @__PURE__ */ h("div", { className: "users-dialog-body", children: [
        /* @__PURE__ */ e(T, { label: "Role", children: /* @__PURE__ */ e(A, { "aria-label": "Role", options: c, value: s, onValueChange: a }) }),
        m !== null ? /* @__PURE__ */ e(w, { message: m }) : null,
        /* @__PURE__ */ h("div", { className: "users-dialog-actions", children: [
          /* @__PURE__ */ e(b, { type: "button", onClick: t, children: "Cancel" }),
          /* @__PURE__ */ h(b, { type: "button", variant: "primary", disabled: !g, onClick: y, children: [
            u ? /* @__PURE__ */ e(U, { label: "Saving role" }) : null,
            "Save"
          ] })
        ] })
      ] })
    }
  );
}
function ne({
  user: n,
  api: i,
  onClose: r,
  onDone: t
}) {
  const [l, s] = o(!1), [a, u] = o(null), [d, m] = o(null), p = k(() => {
    s(!0), u(null), i.regenerateInvite(n.user_id).then(
      (c) => {
        m(c), s(!1), t();
      },
      (c) => {
        u(N(c)), s(!1);
      }
    );
  }, [i, n.user_id, t]);
  return d !== null ? /* @__PURE__ */ h(
    D,
    {
      title: "Invite regenerated",
      open: !0,
      onOpenChange: (c) => {
        c || r();
      },
      children: [
        /* @__PURE__ */ e(P, { result: d }),
        /* @__PURE__ */ e("div", { className: "users-dialog-actions", style: { marginTop: "var(--tai-space-4)" }, children: /* @__PURE__ */ e(b, { type: "button", variant: "primary", onClick: r, children: "Done" }) })
      ]
    }
  ) : /* @__PURE__ */ e(
    D,
    {
      title: `Regenerate invite — ${n.email}`,
      open: !0,
      onOpenChange: (c) => {
        c || r();
      },
      children: /* @__PURE__ */ h("div", { className: "users-dialog-body", children: [
        /* @__PURE__ */ e("p", { style: { margin: 0 }, children: "This replaces the current invite link. The old link stops working immediately." }),
        a !== null ? /* @__PURE__ */ e(w, { message: a }) : null,
        /* @__PURE__ */ h("div", { className: "users-dialog-actions", children: [
          /* @__PURE__ */ e(b, { type: "button", onClick: r, children: "Cancel" }),
          /* @__PURE__ */ h(b, { type: "button", variant: "primary", disabled: l, onClick: p, children: [
            l ? /* @__PURE__ */ e(U, { label: "Regenerating invite" }) : null,
            "Regenerate"
          ] })
        ] })
      ] })
    }
  );
}
function te({
  action: n,
  roles: i,
  api: r,
  onClose: t,
  onFinish: l,
  onReload: s
}) {
  return n === null ? null : n.kind === "role" ? /* @__PURE__ */ e(ee, { user: n.user, roles: i, api: r, onClose: t, onDone: l }) : n.kind === "disable" ? /* @__PURE__ */ e(
    _,
    {
      title: n.user.disabled ? "Enable user" : "Disable user",
      confirmLabel: n.user.disabled ? "Enable" : "Disable",
      pendingLabel: n.user.disabled ? "Enabling" : "Disabling",
      confirmVariant: n.user.disabled ? "primary" : "danger",
      run: () => r.setDisabled(n.user.user_id, !n.user.disabled),
      onClose: t,
      onDone: l,
      children: n.user.disabled ? `Re-enable ${n.user.email}? Their sessions were revoked when they were disabled and must sign in again.` : `Disable ${n.user.email}? This revokes their sessions and API keys immediately.`
    }
  ) : n.kind === "invite" ? /* @__PURE__ */ e(ne, { user: n.user, api: r, onClose: t, onDone: s }) : /* @__PURE__ */ e(
    _,
    {
      title: "Delete user",
      confirmLabel: "Delete",
      pendingLabel: "Deleting",
      run: () => r.deleteUser(n.user.user_id),
      onClose: t,
      onDone: l,
      children: `Delete ${n.user.email}? This removes their account, sessions, invites, and access-control policy. This cannot be undone.`
    }
  );
}
function ie(n) {
  const { api: i, users: r, roles: t, loadError: l, loading: s, reload: a } = W(), [u, d] = o(!1), [m, p] = o(null), c = k(() => {
    p(null);
  }, []), g = k(() => {
    p(null), a();
  }, [a]);
  return /* @__PURE__ */ e("div", { className: "tai42_accounts_postgres-root", children: /* @__PURE__ */ h("div", { className: "users-page", children: [
    /* @__PURE__ */ h("div", { className: "users-toolbar", children: [
      /* @__PURE__ */ e("h1", { className: "users-toolbar-title", children: "Users" }),
      /* @__PURE__ */ e(
        b,
        {
          type: "button",
          variant: "primary",
          onClick: () => {
            d(!0);
          },
          children: "Invite user"
        }
      )
    ] }),
    /* @__PURE__ */ e(
      X,
      {
        loading: s,
        users: r,
        loadError: l,
        reload: a,
        onAction: p
      }
    ),
    u ? /* @__PURE__ */ e(
      Z,
      {
        roles: t,
        api: i,
        onClose: () => {
          d(!1);
        },
        onCreated: a
      }
    ) : null,
    /* @__PURE__ */ e(
      te,
      {
        action: m,
        roles: t,
        api: i,
        onClose: c,
        onFinish: g,
        onReload: a
      }
    )
  ] }) });
}
function re() {
  return /* @__PURE__ */ h(
    "svg",
    {
      width: "1em",
      height: "1em",
      viewBox: "0 0 16 16",
      fill: "none",
      stroke: "currentColor",
      strokeWidth: "1.5",
      "aria-hidden": "true",
      children: [
        /* @__PURE__ */ e("circle", { cx: "8", cy: "5", r: "2.5" }),
        /* @__PURE__ */ e("path", { d: "M2.5 13.5a5.5 5.5 0 0 1 11 0", strokeLinecap: "round" })
      ]
    }
  );
}
function oe(n) {
  n.registerPage({ path: "users", title: "Users", component: ie }), n.registerNavEntry({
    path: "users",
    title: "Users",
    icon: re,
    section: "Administration"
  });
}
export {
  oe as register
};
