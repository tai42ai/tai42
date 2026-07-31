import { jsx as e, jsxs as c } from "react/jsx-runtime";
import { useMemo as A, useState as a, useCallback as R, useEffect as H } from "react";
import { useAuth as M, useOnUnauthorized as q, errorMessage as U, Spinner as S, ErrorState as I, EmptyState as z, Table as F, THead as J, TR as L, TH as D, TBody as G, TD as N, Badge as T, Button as v, Dialog as w, Field as O, TextInput as W, Select as x, ConfirmDialog as Y, CopyField as K } from "@tai42/studio-sdk";
async function Q(n, i, l, t) {
  const r = new Headers({ accept: "application/json" });
  i !== null && r.set("x-api-key", i), t?.body !== void 0 && r.set("content-type", "application/json");
  const o = await fetch(n, {
    method: t?.method ?? "GET",
    headers: r,
    body: t?.body !== void 0 ? JSON.stringify(t.body) : void 0,
    signal: t?.signal
  });
  if (o.status === 401)
    throw l(), new Error("Your session has expired — sign in again.");
  const d = await o.text(), u = d === "" ? {} : JSON.parse(d);
  if (!o.ok) {
    const p = typeof u.error == "string" && u.error.length > 0 ? u.error : `Request failed (${String(o.status)})`;
    throw new Error(p);
  }
  return u.data;
}
function X() {
  const { token: n } = M(), i = q();
  return A(() => {
    const l = (t, r) => Q(t, n, i, r);
    return {
      listUsers: (t) => l("/api/auth/users", { signal: t }).then((r) => r.users),
      listRoles: (t) => l("/api/auth/roles", { signal: t }),
      createUser: (t) => l("/api/auth/users", { method: "POST", body: t }),
      setRole: (t, r) => l(`/api/auth/users/${encodeURIComponent(t)}`, {
        method: "PUT",
        body: { role: r }
      }).then(() => {
      }),
      setDisabled: (t, r) => l(`/api/auth/users/${encodeURIComponent(t)}`, {
        method: "PUT",
        body: { disabled: r }
      }).then(() => {
      }),
      deleteUser: (t) => l(`/api/auth/users/${encodeURIComponent(t)}`, {
        method: "DELETE"
      }).then(() => {
      }),
      regenerateInvite: (t) => l(`/api/auth/users/${encodeURIComponent(t)}/invite`, {
        method: "POST"
      })
    };
  }, [n, i]);
}
function Z({ user: n }) {
  return n.pending_invite ? /* @__PURE__ */ e(T, { variant: "warning", children: "Invite pending" }) : n.disabled ? /* @__PURE__ */ e(T, { variant: "danger", children: "Disabled" }) : /* @__PURE__ */ e(T, { variant: "success", children: "Active" });
}
function ee(n) {
  const i = new Date(n);
  return Number.isNaN(i.getTime()) ? n : i.toLocaleDateString();
}
function B({ result: n }) {
  return /* @__PURE__ */ e("div", { className: "users-dialog-body", children: /* @__PURE__ */ e(
    K,
    {
      label: "Invite link",
      value: n.login_path,
      caption: "Copy this link now — it is shown only once. Send it to the user; opening it lets them set a password and sign in."
    }
  ) });
}
function $({
  title: n,
  confirmLabel: i,
  pendingLabel: l,
  confirmVariant: t,
  run: r,
  onClose: o,
  onDone: d,
  children: u
}) {
  const [p, m] = a(!1), [f, h] = a(null), C = R(() => {
    m(!0), h(null), r().then(
      () => {
        d();
      },
      (y) => {
        h(y instanceof Error ? y : new Error(String(y))), m(!1);
      }
    );
  }, [r, d]);
  return /* @__PURE__ */ e(
    Y,
    {
      title: n,
      confirmLabel: i,
      pendingLabel: l,
      confirmVariant: t,
      isPending: p,
      error: f,
      onConfirm: C,
      onClose: o,
      children: u
    }
  );
}
function ne({
  roles: n,
  api: i,
  onClose: l,
  onCreated: t
}) {
  const [r, o] = a(""), [d, u] = a(n[0]?.name ?? ""), [p, m] = a(!1), [f, h] = a(null), [C, y] = a(null), s = A(() => n.map((g) => ({ value: g.name, label: g.name })), [n]), E = r.trim().length > 0 && d.length > 0 && !p, k = R(() => {
    m(!0), h(null), i.createUser({ email: r.trim(), role: d }).then(
      (g) => {
        y(g), m(!1), t();
      },
      (g) => {
        h(U(g)), m(!1);
      }
    );
  }, [i, r, d, t]);
  return C !== null ? /* @__PURE__ */ c(
    w,
    {
      title: "Invite created",
      open: !0,
      onOpenChange: (g) => {
        g || l();
      },
      children: [
        /* @__PURE__ */ e(B, { result: C }),
        /* @__PURE__ */ e("div", { className: "users-dialog-actions", style: { marginTop: "var(--tai-space-4)" }, children: /* @__PURE__ */ e(v, { type: "button", variant: "primary", onClick: l, children: "Done" }) })
      ]
    }
  ) : /* @__PURE__ */ e(
    w,
    {
      title: "Invite user",
      open: !0,
      onOpenChange: (g) => {
        g || l();
      },
      children: /* @__PURE__ */ c("div", { className: "users-dialog-body", children: [
        /* @__PURE__ */ e(O, { label: "Email", children: /* @__PURE__ */ e(
          W,
          {
            type: "email",
            "aria-label": "Email",
            value: r,
            onChange: (g) => {
              o(g.target.value);
            }
          }
        ) }),
        /* @__PURE__ */ e(O, { label: "Role", children: /* @__PURE__ */ e(
          x,
          {
            "aria-label": "Role",
            options: s,
            value: d,
            onValueChange: u,
            placeholder: s.length === 0 ? "No roles available" : "Select a role",
            disabled: s.length === 0
          }
        ) }),
        f !== null ? /* @__PURE__ */ e(I, { message: f }) : null,
        /* @__PURE__ */ c("div", { className: "users-dialog-actions", children: [
          /* @__PURE__ */ e(v, { type: "button", onClick: l, children: "Cancel" }),
          /* @__PURE__ */ c(v, { type: "button", variant: "primary", disabled: !E, onClick: k, children: [
            p ? /* @__PURE__ */ e(S, { label: "Creating invite" }) : null,
            "Send invite"
          ] })
        ] })
      ] })
    }
  );
}
function te({
  user: n,
  roles: i,
  api: l,
  onClose: t,
  onDone: r
}) {
  const [o, d] = a(n.role), [u, p] = a(!1), [m, f] = a(null), h = A(() => i.map((s) => ({ value: s.name, label: s.name })), [i]), C = o !== n.role && !u, y = R(() => {
    p(!0), f(null), l.setRole(n.user_id, o).then(
      () => {
        r();
      },
      (s) => {
        f(U(s)), p(!1);
      }
    );
  }, [l, n.user_id, o, r]);
  return /* @__PURE__ */ e(
    w,
    {
      title: `Change role — ${n.email}`,
      open: !0,
      onOpenChange: (s) => {
        s || t();
      },
      children: /* @__PURE__ */ c("div", { className: "users-dialog-body", children: [
        /* @__PURE__ */ e(O, { label: "Role", children: /* @__PURE__ */ e(x, { "aria-label": "Role", options: h, value: o, onValueChange: d }) }),
        m !== null ? /* @__PURE__ */ e(I, { message: m }) : null,
        /* @__PURE__ */ c("div", { className: "users-dialog-actions", children: [
          /* @__PURE__ */ e(v, { type: "button", onClick: t, children: "Cancel" }),
          /* @__PURE__ */ c(v, { type: "button", variant: "primary", disabled: !C, onClick: y, children: [
            u ? /* @__PURE__ */ e(S, { label: "Saving role" }) : null,
            "Save"
          ] })
        ] })
      ] })
    }
  );
}
function ie({
  user: n,
  api: i,
  onClose: l,
  onDone: t
}) {
  const [r, o] = a(!1), [d, u] = a(null), [p, m] = a(null), f = R(() => {
    o(!0), u(null), i.regenerateInvite(n.user_id).then(
      (h) => {
        m(h), o(!1), t();
      },
      (h) => {
        u(U(h)), o(!1);
      }
    );
  }, [i, n.user_id, t]);
  return p !== null ? /* @__PURE__ */ c(
    w,
    {
      title: "Invite regenerated",
      open: !0,
      onOpenChange: (h) => {
        h || l();
      },
      children: [
        /* @__PURE__ */ e(B, { result: p }),
        /* @__PURE__ */ e("div", { className: "users-dialog-actions", style: { marginTop: "var(--tai-space-4)" }, children: /* @__PURE__ */ e(v, { type: "button", variant: "primary", onClick: l, children: "Done" }) })
      ]
    }
  ) : /* @__PURE__ */ e(
    w,
    {
      title: `Regenerate invite — ${n.email}`,
      open: !0,
      onOpenChange: (h) => {
        h || l();
      },
      children: /* @__PURE__ */ c("div", { className: "users-dialog-body", children: [
        /* @__PURE__ */ e("p", { style: { margin: 0 }, children: "This replaces the current invite link. The old link stops working immediately." }),
        d !== null ? /* @__PURE__ */ e(I, { message: d }) : null,
        /* @__PURE__ */ c("div", { className: "users-dialog-actions", children: [
          /* @__PURE__ */ e(v, { type: "button", onClick: l, children: "Cancel" }),
          /* @__PURE__ */ c(v, { type: "button", variant: "primary", disabled: r, onClick: f, children: [
            r ? /* @__PURE__ */ e(S, { label: "Regenerating invite" }) : null,
            "Regenerate"
          ] })
        ] })
      ] })
    }
  );
}
function le({
  user: n,
  onAction: i
}) {
  return /* @__PURE__ */ c("div", { className: "users-row-actions", children: [
    /* @__PURE__ */ e(v, { type: "button", onClick: () => i({ kind: "role", user: n }), children: "Change role" }),
    /* @__PURE__ */ e(v, { type: "button", onClick: () => i({ kind: "disable", user: n }), children: n.disabled ? "Enable" : "Disable" }),
    n.pending_invite ? /* @__PURE__ */ e(v, { type: "button", onClick: () => i({ kind: "invite", user: n }), children: "Regenerate invite" }) : null,
    /* @__PURE__ */ e(v, { type: "button", variant: "danger", onClick: () => i({ kind: "delete", user: n }), children: "Delete" })
  ] });
}
function re(n) {
  const i = X(), [l, t] = a(null), [r, o] = a([]), [d, u] = a(null), [p, m] = a(!0), [f, h] = a(0), [C, y] = a(!1), [s, E] = a(null), k = R(() => {
    h((b) => b + 1);
  }, []), g = R(() => {
    E(null);
  }, []), _ = R(() => {
    E(null), k();
  }, [k]);
  H(() => {
    const b = new AbortController();
    return m(!0), u(null), Promise.all([i.listUsers(b.signal), i.listRoles(b.signal)]).then(
      ([P, j]) => {
        b.signal.aborted || (t(P), o(j), m(!1));
      },
      (P) => {
        b.signal.aborted || (u(U(P)), m(!1));
      }
    ), () => {
      b.abort();
    };
  }, [i, f]);
  const V = p && l === null ? /* @__PURE__ */ e(S, { label: "Loading users" }) : d !== null && l === null ? /* @__PURE__ */ e(I, { message: d, onRetry: k }) : l !== null && l.length === 0 ? /* @__PURE__ */ e(
    z,
    {
      title: "No users yet",
      description: "Invite the first user to get them a one-time sign-in link."
    }
  ) : /* @__PURE__ */ c(F, { children: [
    /* @__PURE__ */ e(J, { children: /* @__PURE__ */ c(L, { children: [
      /* @__PURE__ */ e(D, { children: "Email" }),
      /* @__PURE__ */ e(D, { children: "Role" }),
      /* @__PURE__ */ e(D, { children: "Status" }),
      /* @__PURE__ */ e(D, { children: "Created" }),
      /* @__PURE__ */ e(D, { children: /* @__PURE__ */ e("span", { className: "users-cell-muted", children: "Actions" }) })
    ] }) }),
    /* @__PURE__ */ e(G, { children: (l ?? []).map((b) => /* @__PURE__ */ c(L, { children: [
      /* @__PURE__ */ e(N, { children: b.email }),
      /* @__PURE__ */ e(N, { children: /* @__PURE__ */ e(T, { variant: "primary", children: b.role }) }),
      /* @__PURE__ */ e(N, { children: /* @__PURE__ */ e(Z, { user: b }) }),
      /* @__PURE__ */ e(N, { children: /* @__PURE__ */ e("span", { className: "users-cell-muted", children: ee(b.created_at) }) }),
      /* @__PURE__ */ e(N, { children: /* @__PURE__ */ e(le, { user: b, onAction: E }) })
    ] }, b.user_id)) })
  ] });
  return /* @__PURE__ */ e("div", { className: "tai42_accounts_postgres-root", children: /* @__PURE__ */ c("div", { className: "users-page", children: [
    /* @__PURE__ */ c("div", { className: "users-toolbar", children: [
      /* @__PURE__ */ e("h1", { className: "users-toolbar-title", children: "Users" }),
      /* @__PURE__ */ e(
        v,
        {
          type: "button",
          variant: "primary",
          onClick: () => {
            y(!0);
          },
          children: "Invite user"
        }
      )
    ] }),
    V,
    C ? /* @__PURE__ */ e(
      ne,
      {
        roles: r,
        api: i,
        onClose: () => {
          y(!1);
        },
        onCreated: k
      }
    ) : null,
    s?.kind === "role" ? /* @__PURE__ */ e(
      te,
      {
        user: s.user,
        roles: r,
        api: i,
        onClose: g,
        onDone: _
      }
    ) : null,
    s?.kind === "disable" ? /* @__PURE__ */ e(
      $,
      {
        title: s.user.disabled ? "Enable user" : "Disable user",
        confirmLabel: s.user.disabled ? "Enable" : "Disable",
        pendingLabel: s.user.disabled ? "Enabling" : "Disabling",
        confirmVariant: s.user.disabled ? "primary" : "danger",
        run: () => i.setDisabled(s.user.user_id, !s.user.disabled),
        onClose: g,
        onDone: _,
        children: s.user.disabled ? `Re-enable ${s.user.email}? Their sessions were revoked when they were disabled and must sign in again.` : `Disable ${s.user.email}? This revokes their sessions and API keys immediately.`
      }
    ) : null,
    s?.kind === "invite" ? /* @__PURE__ */ e(
      ie,
      {
        user: s.user,
        api: i,
        onClose: g,
        onDone: k
      }
    ) : null,
    s?.kind === "delete" ? /* @__PURE__ */ e(
      $,
      {
        title: "Delete user",
        confirmLabel: "Delete",
        pendingLabel: "Deleting",
        run: () => i.deleteUser(s.user.user_id),
        onClose: g,
        onDone: _,
        children: `Delete ${s.user.email}? This removes their account, sessions, invites, and access-control policy. This cannot be undone.`
      }
    ) : null
  ] }) });
}
function se() {
  return /* @__PURE__ */ c(
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
function ce(n) {
  n.registerPage({ path: "users", title: "Users", component: re }), n.registerNavEntry({
    path: "users",
    title: "Users",
    icon: se,
    section: "Administration"
  });
}
export {
  ce as register
};
