import { Button as e, Card as t, Checkbox as n, ConfirmDialog as r, CopyField as i, Dialog as a, EmptyState as o, ErrorState as s, Field as c, ScrollRegion as l, Skeleton as u, Spinner as d, TBody as f, TD as p, TH as m, THead as h, TR as g, Table as _, TextInput as v, coversAnyRoute as y, errorMessage as b, isFullProjection as x, useApi as ee, useCapabilities as te, useOnUnauthorized as ne } from "@tai42/studio-sdk";
import * as S from "react";
import { useRef as re, useState as C } from "react";
import { jsx as w, jsxs as T } from "react/jsx-runtime";
// studio-src/mount-base.ts
var ie = null;
function ae(e) {
	ie = e;
}
function oe() {
	return ie;
}
// node_modules/.pnpm/@tanstack+react-query@5.104.5_@types+react@19.3.0_react@19.3.1/node_modules/@tanstack/react-query/build/modern/QueryClientProvider.js
var se = S.createContext(void 0), ce = (e) => {
	let t = S.useContext(se);
	if (e) return e;
	if (!t) throw Error("No QueryClient set, use QueryClientProvider to set one");
	return t;
}, le = ({ client: e, children: t }) => (S.useEffect(() => (e.mount(), () => {
	e.unmount();
}), [e]), /* @__PURE__ */ w(se.Provider, {
	value: e,
	children: t
})), ue = {
	setTimeout: (e, t) => setTimeout(e, t),
	clearTimeout: (e) => clearTimeout(e),
	setInterval: (e, t) => setInterval(e, t),
	clearInterval: (e) => clearInterval(e)
}, de = new class {
	#e = ue;
	setTimeoutProvider(e) {
		this.#e = e;
	}
	setTimeout(e, t) {
		return this.#e.setTimeout(e, t);
	}
	clearTimeout(e) {
		this.#e.clearTimeout(e);
	}
	setInterval(e, t) {
		return this.#e.setInterval(e, t);
	}
	clearInterval(e) {
		this.#e.clearInterval(e);
	}
}();
function fe(e) {
	setTimeout(e, 0);
}
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/utils.js
var pe = typeof window > "u" || "Deno" in globalThis;
function E() {}
function me(e, t) {
	return typeof e == "function" ? e(t) : e;
}
function he(e) {
	return typeof e == "number" && e >= 0 && e !== Infinity;
}
function ge(e, t) {
	return Math.max(e + (t || 0) - Date.now(), 0);
}
function D(e, t) {
	return typeof e == "function" ? e(t) : e;
}
function _e(e, t) {
	let { type: n = "all", exact: r, fetchStatus: i, predicate: a, queryKey: o, stale: s } = e;
	if (o) {
		if (r) {
			if (t.queryHash !== ye(o, t.options)) return !1;
		} else if (!xe(t.queryKey, o)) return !1;
	}
	if (n !== "all") {
		let e = t.isActive();
		if (n === "active" && !e || n === "inactive" && e) return !1;
	}
	return !(typeof s == "boolean" && t.isStale() !== s || i && i !== t.state.fetchStatus || a && !a(t));
}
function ve(e, t) {
	let { exact: n, status: r, predicate: i, mutationKey: a } = e;
	if (a) {
		if (!t.options.mutationKey) return !1;
		if (n) {
			if (be(t.options.mutationKey) !== be(a)) return !1;
		} else if (!xe(t.options.mutationKey, a)) return !1;
	}
	return !(r && t.state.status !== r || i && !i(t));
}
function ye(e, t) {
	return (t?.queryKeyHashFn || be)(e);
}
function be(e) {
	return JSON.stringify(e, (e, t) => Ee(t) ? Object.keys(t).sort().reduce((e, n) => (e[n] = t[n], e), {}) : t);
}
function xe(e, t) {
	if (e === t) return !0;
	if (typeof e != typeof t) return !1;
	if (e && t && typeof e == "object" && typeof t == "object") {
		if (Array.isArray(e) && Array.isArray(t)) {
			if (t.length > e.length) return !1;
			for (let n = 0; n < t.length; n++) if (!xe(e[n], t[n])) return !1;
			return !0;
		}
		let n = Object.keys(t);
		for (let r of n) if (!xe(e[r], t[r])) return !1;
		return !0;
	}
	return !1;
}
var Se = Object.prototype.hasOwnProperty;
function Ce(e, t, n = 0) {
	if (e === t) return e;
	if (n > 500) return t;
	let r = Te(e) && Te(t);
	if (!r && !(Ee(e) && Ee(t))) return t;
	let i = (r ? e : Object.keys(e)).length, a = r ? t : Object.keys(t), o = a.length, s = r ? Array(o) : {}, c = 0;
	for (let l = 0; l < o; l++) {
		let o = r ? l : a[l], u = e[o], d = t[o];
		if (u === d) {
			s[o] = u, (r ? l < i : Se.call(e, o)) && c++;
			continue;
		}
		if (u === null || d === null || typeof u != "object" || typeof d != "object") {
			s[o] = d;
			continue;
		}
		let f = Ce(u, d, n + 1);
		s[o] = f, f === u && c++;
	}
	return i === o && c === i ? e : s;
}
function we(e, t) {
	if (!t || Object.keys(e).length !== Object.keys(t).length) return !1;
	for (let n in e) if (e[n] !== t[n]) return !1;
	return !0;
}
function Te(e) {
	return Array.isArray(e) && e.length === Object.keys(e).length;
}
function Ee(e) {
	if (!De(e)) return !1;
	let t = Object.getPrototypeOf(e), n = t?.constructor;
	if (n === void 0) return !0;
	if (typeof n != "function") return !1;
	let r = n.prototype;
	return !(!De(r) || !r.hasOwnProperty("isPrototypeOf") || t !== Object.prototype);
}
function De(e) {
	return Object.prototype.toString.call(e) === "[object Object]";
}
function Oe(e) {
	return new Promise((t) => {
		de.setTimeout(t, e);
	});
}
function ke(e, t, n) {
	return typeof n.structuralSharing == "function" ? n.structuralSharing(e, t) : n.structuralSharing === !1 ? t : Ce(e, t);
}
function Ae(e, t, n = 0) {
	let r = [...e, t];
	return n && r.length > n ? r.slice(1) : r;
}
function je(e, t, n = 0) {
	let r = [t, ...e];
	return n && r.length > n ? r.slice(0, -1) : r;
}
var Me = Symbol();
function Ne(e, t) {
	return !e.queryFn && t?.initialPromise ? () => t.initialPromise : !e.queryFn || e.queryFn === Me ? () => Promise.reject(/* @__PURE__ */ Error(`Missing queryFn: '${e.queryHash}'`)) : e.queryFn;
}
function Pe(e, t) {
	return typeof e == "function" ? e(...t) : !!e;
}
function Fe(e, t, n) {
	let r = !1, i;
	return Object.defineProperty(e, "signal", {
		enumerable: !0,
		get: () => (i ??= t(), r || (r = !0, i.aborted ? n() : i.addEventListener("abort", n, { once: !0 })), i)
	}), e;
}
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/environmentManager.js
var Ie = () => pe, Le = () => Ie(), Re = class {
	constructor() {
		this.listeners = /* @__PURE__ */ new Set(), this.subscribe = this.subscribe.bind(this);
	}
	subscribe(e) {
		return this.listeners.add(e), this.onSubscribe(), () => {
			this.listeners.delete(e), this.onUnsubscribe();
		};
	}
	hasListeners() {
		return this.listeners.size > 0;
	}
	onSubscribe() {}
	onUnsubscribe() {}
}, ze = new class extends Re {
	#e;
	#t;
	#n;
	constructor() {
		super(), this.#n = (e) => {
			if (typeof window < "u" && window.addEventListener) {
				let t = () => e();
				return window.addEventListener("visibilitychange", t, !1), () => {
					window.removeEventListener("visibilitychange", t);
				};
			}
		};
	}
	onSubscribe() {
		this.#t || this.setEventListener(this.#n);
	}
	onUnsubscribe() {
		this.hasListeners() || (this.#t?.(), this.#t = void 0);
	}
	setEventListener(e) {
		this.#n = e, this.#t?.(), this.#t = e((e) => {
			typeof e == "boolean" ? this.setFocused(e) : this.onFocus();
		});
	}
	setFocused(e) {
		this.#e !== e && (this.#e = e, this.onFocus());
	}
	onFocus() {
		let e = this.isFocused();
		this.listeners.forEach((t) => {
			t(e);
		});
	}
	isFocused() {
		return typeof this.#e == "boolean" ? this.#e : globalThis.document?.visibilityState !== "hidden";
	}
}(), Be = fe;
function Ve() {
	let e = [], t = 0, n = (e) => {
		e();
	}, r = (e) => {
		e();
	}, i = Be, a = (r) => {
		t ? e.push(r) : i(() => {
			n(r);
		});
	}, o = () => {
		let t = e;
		e = [], t.length && i(() => {
			r(() => {
				t.forEach((e) => {
					n(e);
				});
			});
		});
	};
	return {
		batch: (e) => {
			let n;
			t++;
			try {
				n = e();
			} finally {
				t--, t || o();
			}
			return n;
		},
		batchCalls: (e) => (...t) => {
			a(() => {
				e(...t);
			});
		},
		schedule: a,
		setNotifyFunction: (e) => {
			n = e;
		},
		setBatchNotifyFunction: (e) => {
			r = e;
		},
		setScheduler: (e) => {
			i = e;
		}
	};
}
var O = Ve(), He = new class extends Re {
	#e = !0;
	#t;
	#n;
	constructor() {
		super(), this.#n = (e) => {
			if (typeof window < "u" && window.addEventListener) {
				let t = () => e(!0), n = () => e(!1);
				return window.addEventListener("online", t, !1), window.addEventListener("offline", n, !1), () => {
					window.removeEventListener("online", t), window.removeEventListener("offline", n);
				};
			}
		};
	}
	onSubscribe() {
		this.#t || this.setEventListener(this.#n);
	}
	onUnsubscribe() {
		this.hasListeners() || (this.#t?.(), this.#t = void 0);
	}
	setEventListener(e) {
		this.#n = e, this.#t?.(), this.#t = e(this.setOnline.bind(this));
	}
	setOnline(e) {
		this.#e !== e && (this.#e = e, this.listeners.forEach((t) => {
			t(e);
		}));
	}
	isOnline() {
		return this.#e;
	}
}();
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/retryer.js
function Ue(e) {
	return Math.min(1e3 * 2 ** e, 3e4);
}
function We(e) {
	return (e ?? "online") !== "online" || He.isOnline();
}
var Ge = class extends Error {
	constructor(e) {
		super("CancelledError"), this.revert = e?.revert, this.silent = e?.silent;
	}
};
function Ke(e) {
	let t = !1, n = 0, r, i = "pending", a, o, s = new Promise((e, t) => {
		a = e, o = t;
	});
	s.catch(E);
	let c = () => i !== "pending", l = (t) => {
		if (!c()) {
			let n = new Ge(t);
			h(n), e.onCancel?.(n);
		}
	}, u = () => {
		t = !0;
	}, d = () => {
		t = !1;
	}, f = () => ze.isFocused() && (e.networkMode === "always" || He.isOnline()) && e.canRun(), p = () => We(e.networkMode) && e.canRun(), m = (e) => {
		c() || (r?.(), i = "resolved", a(e));
	}, h = (e) => {
		c() || (r?.(), i = "rejected", o(e));
	}, g = (t) => new Promise((n) => {
		r = (e) => {
			(c() || t()) && n(e);
		}, e.onPause?.();
	}).then(() => {
		r = void 0, c() || e.onContinue?.();
	}), _ = () => {
		if (c()) return;
		let r, i = n === 0 ? e.initialPromise : void 0;
		try {
			r = i ?? e.fn();
		} catch (e) {
			r = Promise.reject(e);
		}
		Promise.resolve(r).then(m).catch((r) => {
			if (c()) return;
			let i = e.retry ?? (Le() ? 0 : 3), a = e.retryDelay ?? Ue, o = typeof a == "function" ? a(n, r) : a, s = i === !0 || typeof i == "number" && n < i || typeof i == "function" && i(n, r);
			t || !s ? h(r) : (n++, e.onFail?.(n, r), Oe(o).then(() => f() ? void 0 : g(f)).then(() => {
				t ? h(r) : _();
			}));
		});
	};
	return {
		promise: s,
		status: () => i,
		cancel: l,
		continue: () => (r?.(), s),
		cancelRetry: u,
		continueRetry: d,
		canStart: p,
		start: () => (p() ? _() : g(p).then(_), s)
	};
}
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/removable.js
var qe = class {
	#e;
	destroy() {
		this.clearGcTimeout();
	}
	scheduleGc() {
		this.clearGcTimeout(), he(this.gcTime) && (this.#e = de.setTimeout(() => {
			this.optionalRemove();
		}, this.gcTime));
	}
	updateGcTime(e) {
		this.gcTime = Math.max(this.gcTime || 0, e ?? (Le() ? Infinity : 3e5));
	}
	clearGcTimeout() {
		this.#e !== void 0 && (de.clearTimeout(this.#e), this.#e = void 0);
	}
};
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/infiniteQueryBehavior.js
function Je(e) {
	return { onFetch: (t, n) => {
		let r = t.options, i = t.fetchOptions?.meta?.fetchMore?.direction, a = t.state.data?.pages || [], o = t.state.data?.pageParams || [], s = {
			pages: [],
			pageParams: []
		}, c = 0, l = async () => {
			let n = !1, l = (e) => {
				Fe(e, () => t.signal, () => n = !0);
			}, u = Ne(t.options, t.fetchOptions), d = async (e, r, i) => {
				if (n) return Promise.reject(t.signal.reason);
				if (r == null && e.pages.length) return Promise.resolve(e);
				let a = (() => {
					let e = {
						client: t.client,
						queryKey: t.queryKey,
						pageParam: r,
						direction: i ? "backward" : "forward",
						meta: t.options.meta
					};
					return l(e), e;
				})(), o = await u(a), { maxPages: s } = t.options, c = i ? je : Ae;
				return {
					pages: c(e.pages, o, s),
					pageParams: c(e.pageParams, r, s)
				};
			};
			if (i && a.length) {
				let e = i === "backward", t = e ? Xe : Ye, n = {
					pages: a,
					pageParams: o
				};
				s = await d(n, t(r, n), e);
			} else {
				let t = e ?? a.length;
				do {
					let e = c === 0 ? o[0] === void 0 ? r.initialPageParam : o[0] : Ye(r, s);
					if (c > 0 && e == null) break;
					s = await d(s, e), c++;
				} while (c < t);
			}
			return s;
		};
		t.fetchFn = t.options.persister ? () => t.options.persister?.(l, {
			client: t.client,
			queryKey: t.queryKey,
			meta: t.options.meta,
			signal: t.signal
		}, n) : l;
	} };
}
function Ye(e, { pages: t, pageParams: n }) {
	let r = t.length - 1;
	return t.length > 0 ? e.getNextPageParam(t[r], t, n[r], n) : void 0;
}
function Xe(e, { pages: t, pageParams: n }) {
	return t.length > 0 ? e.getPreviousPageParam?.(t[0], t, n[0], n) : void 0;
}
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/query.js
var Ze = class extends qe {
	#e;
	#t;
	#n;
	#r;
	#i;
	#a;
	#o;
	#s;
	constructor(e) {
		super(), this.#s = !1, this.#o = e.defaultOptions, this.setOptions(e.options), this.observers = [], this.#i = e.client, this.#r = this.#i.getQueryCache(), this.queryKey = e.queryKey, this.queryHash = e.queryHash, this.#t = et(this.options), this.state = e.state ?? this.#t, this.scheduleGc();
	}
	get meta() {
		return this.options.meta;
	}
	get queryType() {
		return this.#e;
	}
	get promise() {
		return this.#a?.promise;
	}
	setOptions(e) {
		if (this.options = {
			...this.#o,
			...e
		}, e?._type && (this.#e = e._type), this.updateGcTime(this.options.gcTime), this.state && this.state.data === void 0) {
			let e = et(this.options);
			e.data !== void 0 && (this.setState($e(e.data, e.dataUpdatedAt)), this.#t = e);
		}
	}
	optionalRemove() {
		!this.observers.length && this.state.fetchStatus === "idle" && this.#r.remove(this);
	}
	setData(e, t) {
		let n = ke(this.state.data, e, this.options);
		return this.#c({
			data: n,
			type: "success",
			dataUpdatedAt: t?.updatedAt,
			manual: t?.manual
		}), n;
	}
	setState(e) {
		this.#c({
			type: "setState",
			state: e
		});
	}
	cancel(e) {
		let t = this.#a?.promise;
		return this.#a?.cancel(e), t ? t.then(E).catch(E) : Promise.resolve();
	}
	destroy() {
		super.destroy(), this.cancel({ silent: !0 });
	}
	get resetState() {
		return this.#t;
	}
	reset() {
		this.destroy(), this.setState(this.resetState);
	}
	isActive() {
		return this.observers.some((e) => D(e.options.enabled, this) !== !1);
	}
	isDisabled() {
		return this.getObserversCount() > 0 ? !this.isActive() : this.options.queryFn === Me || !this.isFetched();
	}
	isFetched() {
		return this.state.dataUpdateCount + this.state.errorUpdateCount > 0;
	}
	isStatic() {
		return this.getObserversCount() > 0 && this.observers.some((e) => D(e.options.staleTime, this) === "static");
	}
	isStale() {
		return this.getObserversCount() > 0 ? this.observers.some((e) => e.getCurrentResult().isStale) : this.state.data === void 0 || this.state.isInvalidated;
	}
	isStaleByTime(e = 0) {
		return this.state.data === void 0 ? !0 : e === "static" ? !1 : this.state.isInvalidated ? !0 : !ge(this.state.dataUpdatedAt, e);
	}
	onFocus() {
		this.observers.find((e) => e.shouldFetchOnWindowFocus())?.refetch({ cancelRefetch: !1 }), this.#a?.continue();
	}
	onOnline() {
		this.observers.find((e) => e.shouldFetchOnReconnect())?.refetch({ cancelRefetch: !1 }), this.#a?.continue();
	}
	addObserver(e) {
		this.observers.includes(e) || (this.observers.push(e), this.clearGcTimeout(), this.#r.notify({
			type: "observerAdded",
			query: this,
			observer: e
		}));
	}
	removeObserver(e) {
		let t = this.observers.indexOf(e);
		t !== -1 && (this.observers.splice(t, 1), this.observers.length || (this.#a && (this.#s || this.state.fetchStatus === "paused" && this.state.status === "pending" ? this.#a.cancel({ revert: !0 }) : this.#a.cancelRetry()), this.scheduleGc()), this.#r.notify({
			type: "observerRemoved",
			query: this,
			observer: e
		}));
	}
	getObserversCount() {
		return this.observers.length;
	}
	invalidate() {
		this.state.isInvalidated || this.#c({ type: "invalidate" });
	}
	async fetch(e, t) {
		if (this.state.fetchStatus !== "idle" && this.#a?.status() !== "rejected") {
			if (this.state.data !== void 0 && t?.cancelRefetch) this.cancel({ silent: !0 });
			else if (this.#a) return this.#a.continueRetry(), this.#a.promise;
		}
		if (e && this.setOptions(e), !this.options.queryFn) {
			let e = this.observers.find((e) => e.options.queryFn);
			e && this.setOptions(e.options);
		}
		let n = new AbortController(), r = (e) => {
			Object.defineProperty(e, "signal", {
				enumerable: !0,
				get: () => (this.#s = !0, n.signal)
			});
		}, i = () => {
			let e = Ne(this.options, t), n = (() => {
				let e = {
					client: this.#i,
					queryKey: this.queryKey,
					meta: this.meta
				};
				return r(e), e;
			})();
			return this.#s = !1, this.options.persister ? this.options.persister(e, n, this) : e(n);
		}, a = (() => {
			let e = {
				fetchOptions: t,
				options: this.options,
				queryKey: this.queryKey,
				client: this.#i,
				state: this.state,
				fetchFn: i
			};
			return r(e), e;
		})();
		(this.#e === "infinite" ? Je(this.options.pages) : this.options.behavior)?.onFetch(a, this), this.#n = this.state, (this.state.fetchStatus === "idle" || this.state.fetchMeta !== a.fetchOptions?.meta) && this.#c({
			type: "fetch",
			meta: a.fetchOptions?.meta
		});
		let o = this.#a = Ke({
			initialPromise: t?.initialPromise,
			fn: a.fetchFn,
			onCancel: (e) => {
				e instanceof Ge && e.revert && this.setState({
					...this.#n,
					fetchStatus: "idle"
				}), n.abort();
			},
			onFail: (e, t) => {
				this.#c({
					type: "failed",
					failureCount: e,
					error: t
				});
			},
			onPause: () => {
				this.#c({ type: "pause" });
			},
			onContinue: () => {
				this.#c({ type: "continue" });
			},
			retry: a.options.retry,
			retryDelay: a.options.retryDelay,
			networkMode: a.options.networkMode,
			canRun: () => !0
		});
		try {
			let e = await o.start();
			if (e === void 0) throw Error(`${this.queryHash} data is undefined`);
			return this.setData(e), this.#r.config.onSuccess?.(e, this), this.#r.config.onSettled?.(e, this.state.error, this), e;
		} catch (e) {
			if (e instanceof Ge) {
				if (e.silent) return this.#a.promise;
				if (e.revert) {
					if (this.state.data === void 0) throw e;
					return this.state.data;
				}
			}
			throw this.#c({
				type: "error",
				error: e
			}), this.#r.config.onError?.(e, this), this.#r.config.onSettled?.(this.state.data, e, this), e;
		} finally {
			this.#a === o && (this.#a = void 0), this.scheduleGc();
		}
	}
	#c(e) {
		let t = (t) => {
			switch (e.type) {
				case "failed": return {
					...t,
					fetchFailureCount: e.failureCount,
					fetchFailureReason: e.error
				};
				case "pause": return {
					...t,
					fetchStatus: "paused"
				};
				case "continue": return {
					...t,
					fetchStatus: "fetching"
				};
				case "fetch": return {
					...t,
					...Qe(t.data, this.options),
					fetchMeta: e.meta ?? null
				};
				case "success":
					let n = {
						...t,
						...$e(e.data, e.dataUpdatedAt),
						dataUpdateCount: t.dataUpdateCount + 1,
						...!e.manual && {
							fetchStatus: "idle",
							fetchFailureCount: 0,
							fetchFailureReason: null
						}
					};
					return this.#n = e.manual ? n : void 0, n;
				case "error":
					let r = e.error;
					return {
						...t,
						error: r,
						errorUpdateCount: t.errorUpdateCount + 1,
						errorUpdatedAt: Date.now(),
						fetchFailureCount: t.fetchFailureCount + 1,
						fetchFailureReason: r,
						fetchStatus: "idle",
						status: "error",
						isInvalidated: !0
					};
				case "invalidate": return {
					...t,
					isInvalidated: !0
				};
				case "setState": return {
					...t,
					...e.state
				};
			}
		};
		this.state = t(this.state), O.batch(() => {
			this.observers.slice().forEach((e) => {
				e.onQueryUpdate();
			}), this.#r.notify({
				query: this,
				type: "updated",
				action: e
			});
		});
	}
};
function Qe(e, t) {
	return {
		fetchFailureCount: 0,
		fetchFailureReason: null,
		fetchStatus: We(t.networkMode) ? "fetching" : "paused",
		...e === void 0 && {
			error: null,
			status: "pending"
		}
	};
}
function $e(e, t) {
	return {
		data: e,
		dataUpdatedAt: t ?? Date.now(),
		error: null,
		isInvalidated: !1,
		status: "success"
	};
}
function et(e) {
	let t = typeof e.initialData == "function" ? e.initialData() : e.initialData, n = t !== void 0, r = n ? typeof e.initialDataUpdatedAt == "function" ? e.initialDataUpdatedAt() : e.initialDataUpdatedAt : 0;
	return {
		data: t,
		dataUpdateCount: 0,
		dataUpdatedAt: n ? r ?? Date.now() : 0,
		error: null,
		errorUpdateCount: 0,
		errorUpdatedAt: 0,
		fetchFailureCount: 0,
		fetchFailureReason: null,
		fetchMeta: null,
		isInvalidated: !1,
		status: n ? "success" : "pending",
		fetchStatus: "idle"
	};
}
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/queryObserver.js
var tt = class extends Re {
	#e;
	#t = void 0;
	#n = void 0;
	#r = void 0;
	#i;
	#a;
	#o;
	#s;
	#c;
	#l;
	#u;
	#d;
	#f;
	#p = /* @__PURE__ */ new Set();
	constructor(e, t) {
		super(), this.options = t, this.#e = e, this.#o = null, this.bindMethods(), this.setOptions(t);
	}
	bindMethods() {
		this.refetch = this.refetch.bind(this);
	}
	onSubscribe() {
		this.listeners.size === 1 && (this.#t.addObserver(this), rt(this.#t, this.options) ? this.#m() : this.updateResult(), this.#y());
	}
	onUnsubscribe() {
		this.hasListeners() || this.destroy();
	}
	shouldFetchOnReconnect() {
		return it(this.#t, this.options, this.options.refetchOnReconnect);
	}
	shouldFetchOnWindowFocus() {
		return it(this.#t, this.options, this.options.refetchOnWindowFocus);
	}
	destroy() {
		this.listeners = /* @__PURE__ */ new Set(), this.#b(), this.#x(), this.#t.removeObserver(this);
	}
	setOptions(e) {
		let t = this.options, n = this.#t;
		if (this.options = this.#e.defaultQueryOptions(e), this.options.enabled !== void 0 && typeof this.options.enabled != "boolean" && typeof this.options.enabled != "function" && typeof D(this.options.enabled, this.#t) != "boolean") throw Error("Expected enabled to be a boolean or a callback that returns a boolean");
		this.#S(), this.#t.setOptions(this.options), t._defaulted && !we(this.options, t) && this.#e.getQueryCache().notify({
			type: "observerOptionsUpdated",
			query: this.#t,
			observer: this
		});
		let r = this.hasListeners();
		r && at(this.#t, n, this.options, t) && this.#m(), this.updateResult(), r && (this.#t !== n || D(this.options.enabled, this.#t) !== D(t.enabled, this.#t) || D(this.options.staleTime, this.#t) !== D(t.staleTime, this.#t)) && this.#g();
		let i = this.#_();
		r && (this.#t !== n || D(this.options.enabled, this.#t) !== D(t.enabled, this.#t) || i !== this.#f) && this.#v(i);
	}
	getOptimisticResult(e) {
		let t = this.#e.getQueryCache().build(this.#e, e), n = this.createResult(t, e);
		return we(this.getCurrentResult(), n) || (this.#r = n, this.#a = this.options, this.#i = this.#t.state), n;
	}
	getCurrentResult() {
		return this.#r;
	}
	trackResult(e, t) {
		return new Proxy(e, { get: (e, n) => (this.trackProp(n), t?.(n), Reflect.get(e, n)) });
	}
	trackProp(e) {
		this.#p.add(e);
	}
	getCurrentQuery() {
		return this.#t;
	}
	refetch({ ...e } = {}) {
		return this.fetch({ ...e });
	}
	fetchOptimistic(e) {
		let t = this.#e.defaultQueryOptions(e), n = this.#e.getQueryCache().build(this.#e, t), r = () => {}, i, a = new Promise((e) => {
			i = e, r = this.#e.getQueryCache().subscribe((i) => {
				i.type === "updated" && i.query.queryHash === n.queryHash && n.state.data !== void 0 && (r(), e(this.createResult(n, t)));
			});
		});
		return Promise.race([n.fetch(t).then(() => {
			let e = this.createResult(n, t);
			return i?.(e), e;
		}).finally(() => {
			r();
		}), a]);
	}
	fetch(e) {
		return this.#m({
			...e,
			cancelRefetch: e.cancelRefetch ?? !0
		}).then(() => (this.updateResult(), this.#r));
	}
	#m(e) {
		this.#S();
		let t = this.#t.fetch(this.options, e);
		return e?.throwOnError || (t = t.catch(E)), t;
	}
	#h(e) {
		return !Le() && D(this.options.enabled, this.#t) !== !1 && he(e);
	}
	#g() {
		this.#b();
		let e = D(this.options.staleTime, this.#t);
		if (this.#r.isStale || !this.#h(e)) return;
		let t = ge(this.#r.dataUpdatedAt, e) + 1;
		this.#u = de.setTimeout(() => {
			this.#r.isStale || this.updateResult();
		}, t);
	}
	#_() {
		return D(this.options.refetchInterval, this.#t) ?? !1;
	}
	#v(e) {
		this.#x(), this.#f = e, this.#f !== 0 && this.#h(this.#f) && (this.#d = de.setInterval(() => {
			(this.options.refetchIntervalInBackground || ze.isFocused()) && this.#m();
		}, this.#f));
	}
	#y() {
		this.#g(), this.#v(this.#_());
	}
	#b() {
		this.#u !== void 0 && (de.clearTimeout(this.#u), this.#u = void 0);
	}
	#x() {
		this.#d !== void 0 && (de.clearInterval(this.#d), this.#d = void 0);
	}
	createResult(e, t) {
		let n = this.#t, r = this.options, i = this.#r, a = this.#i, o = this.#a, s = e === n ? this.#n : e.state, { state: c } = e, l = { ...c }, u = !1, d;
		if (t._optimisticResults) {
			let i = this.hasListeners(), a = !i && rt(e, t), o = i && at(e, n, t, r);
			(a || o) && (l = {
				...l,
				...Qe(c.data, e.options)
			}), t._optimisticResults === "isRestoring" && (l.fetchStatus = "idle");
		}
		let { error: f, errorUpdatedAt: p, status: m } = l;
		d = l.data;
		let h = !1;
		if (t.placeholderData !== void 0 && d === void 0 && m === "pending") {
			let e;
			i?.isPlaceholderData && t.placeholderData === o?.placeholderData ? (e = i.data, h = !0) : e = typeof t.placeholderData == "function" ? t.placeholderData(this.#l?.state.data, this.#l) : t.placeholderData, e !== void 0 && (m = "success", d = ke(i?.data, e, t), u = !0);
		}
		if (t.select && d !== void 0 && !h) {
			if (i && d === a?.data && t.select === this.#s) d = this.#c;
			else try {
				this.#s = t.select, d = t.select(d), d = ke(i?.data, d, t), this.#c = d, this.#o = null;
			} catch (e) {
				this.#o = e;
			}
		} else d === void 0 && (this.#o = null);
		this.#o && (f = this.#o, d = this.#c, p = Date.now(), m = "error", u = !1);
		let g = l.fetchStatus === "fetching", _ = m === "pending", v = m === "error", y = _ && g, b = d !== void 0;
		return {
			status: m,
			fetchStatus: l.fetchStatus,
			isPending: _,
			isSuccess: m === "success",
			isError: v,
			isInitialLoading: y,
			isLoading: y,
			data: d,
			dataUpdatedAt: l.dataUpdatedAt,
			error: f,
			errorUpdatedAt: p,
			failureCount: l.fetchFailureCount,
			failureReason: l.fetchFailureReason,
			errorUpdateCount: l.errorUpdateCount,
			isFetched: e.isFetched(),
			isFetchedAfterMount: l.dataUpdateCount > s.dataUpdateCount || l.errorUpdateCount > s.errorUpdateCount,
			isFetching: g,
			isRefetching: g && !_,
			isLoadingError: v && !b,
			isPaused: l.fetchStatus === "paused",
			isPlaceholderData: u,
			isRefetchError: v && b,
			isStale: ot(e, t),
			refetch: this.refetch,
			isEnabled: D(t.enabled, e) !== !1
		};
	}
	updateResult() {
		let e = this.#r, t = this.createResult(this.#t, this.options);
		if (this.#i = this.#t.state, this.#a = this.options, this.#i.data !== void 0 && (this.#l = this.#t), we(t, e)) return;
		this.#r = t;
		let n = (() => {
			if (!e) return !0;
			let { notifyOnChangeProps: t } = this.options, n = typeof t == "function" ? t() : t;
			if (n === "all" || !n && !this.#p.size) return !0;
			let r = new Set(n ?? this.#p);
			return this.options.throwOnError && r.add("error"), Object.keys(this.#r).some((t) => {
				let n = t;
				return this.#r[n] !== e[n] && r.has(n);
			});
		})();
		O.batch(() => {
			n && this.listeners.forEach((e) => {
				e(this.#r);
			}), this.#e.getQueryCache().notify({
				query: this.#t,
				type: "observerResultsUpdated"
			});
		});
	}
	#S() {
		let e = this.#e.getQueryCache().build(this.#e, this.options);
		if (e === this.#t) return;
		let t = this.#t;
		this.#t = e, this.#n = e.state, this.hasListeners() && (t?.removeObserver(this), e.addObserver(this));
	}
	onQueryUpdate() {
		this.updateResult(), this.hasListeners() && this.#y();
	}
};
function nt(e, t) {
	return D(t.enabled, e) !== !1 && e.state.data === void 0 && (e.state.status !== "error" || D(t.retryOnMount, e) !== !1);
}
function rt(e, t) {
	return nt(e, t) || e.state.data !== void 0 && it(e, t, t.refetchOnMount);
}
function it(e, t, n) {
	if (D(t.enabled, e) !== !1 && D(t.staleTime, e) !== "static") {
		let r = D(n, e);
		return r === "always" || r !== !1 && ot(e, t);
	}
	return !1;
}
function at(e, t, n, r) {
	return (e !== t || D(r.enabled, e) === !1) && (!n.suspense || e.state.status !== "error") && ot(e, n);
}
function ot(e, t) {
	return D(t.enabled, e) !== !1 && e.isStaleByTime(D(t.staleTime, e));
}
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/mutation.js
var st = class extends qe {
	#e;
	#t;
	#n;
	#r;
	constructor(e) {
		super(), this.#e = e.client, this.mutationId = e.mutationId, this.#n = e.mutationCache, this.#t = [], this.state = e.state || ct(), this.setOptions(e.options), this.scheduleGc();
	}
	setOptions(e) {
		this.options = e, this.updateGcTime(this.options.gcTime);
	}
	get meta() {
		return this.options.meta;
	}
	addObserver(e) {
		this.#t.includes(e) || (this.#t.push(e), this.clearGcTimeout(), this.#n.notify({
			type: "observerAdded",
			mutation: this,
			observer: e
		}));
	}
	removeObserver(e) {
		this.#t = this.#t.filter((t) => t !== e), this.scheduleGc(), this.#n.notify({
			type: "observerRemoved",
			mutation: this,
			observer: e
		});
	}
	optionalRemove() {
		this.#t.length || (this.state.status === "pending" ? this.scheduleGc() : this.#n.remove(this));
	}
	continue() {
		return this.#r?.continue() ?? (this.state.status === "pending" ? this.execute(this.state.variables) : Promise.resolve());
	}
	async execute(e) {
		let t = () => {
			this.#i({ type: "continue" });
		}, n = {
			client: this.#e,
			meta: this.options.meta,
			mutationKey: this.options.mutationKey
		}, r = this.#r = Ke({
			fn: () => this.options.mutationFn ? this.options.mutationFn(e, n) : Promise.reject(/* @__PURE__ */ Error("No mutationFn found")),
			onFail: (e, t) => {
				this.#i({
					type: "failed",
					failureCount: e,
					error: t
				});
			},
			onPause: () => {
				this.#i({ type: "pause" });
			},
			onContinue: t,
			retry: this.options.retry ?? 0,
			retryDelay: this.options.retryDelay,
			networkMode: this.options.networkMode,
			canRun: () => this.#n.canRun(this)
		}), i = this.state.status === "pending", a = !r.canStart();
		try {
			if (i) t();
			else {
				this.#i({
					type: "pending",
					variables: e,
					isPaused: a
				}), this.#n.config.onMutate && await this.#n.config.onMutate(e, this, n);
				let t = await this.options.onMutate?.(e, n);
				t !== this.state.context && this.#i({
					type: "pending",
					context: t,
					variables: e,
					isPaused: a
				});
			}
			let o = await r.start();
			return await this.#n.config.onSuccess?.(o, e, this.state.context, this, n), await this.options.onSuccess?.(o, e, this.state.context, n), await this.#n.config.onSettled?.(o, null, this.state.variables, this.state.context, this, n), await this.options.onSettled?.(o, null, e, this.state.context, n), this.#i({
				type: "success",
				data: o
			}), o;
		} catch (t) {
			try {
				await this.#n.config.onError?.(t, e, this.state.context, this, n);
			} catch (e) {
				Promise.reject(e);
			}
			try {
				await this.options.onError?.(t, e, this.state.context, n);
			} catch (e) {
				Promise.reject(e);
			}
			try {
				await this.#n.config.onSettled?.(void 0, t, this.state.variables, this.state.context, this, n);
			} catch (e) {
				Promise.reject(e);
			}
			try {
				await this.options.onSettled?.(void 0, t, e, this.state.context, n);
			} catch (e) {
				Promise.reject(e);
			}
			throw this.#i({
				type: "error",
				error: t
			}), t;
		} finally {
			this.#r === r && (this.#r = void 0), this.#n.runNext(this);
		}
	}
	#i(e) {
		let t = (t) => {
			switch (e.type) {
				case "failed": return {
					...t,
					failureCount: e.failureCount,
					failureReason: e.error
				};
				case "pause": return {
					...t,
					isPaused: !0
				};
				case "continue": return {
					...t,
					isPaused: !1
				};
				case "pending": return {
					...t,
					context: e.context,
					data: void 0,
					failureCount: 0,
					failureReason: null,
					error: null,
					isPaused: e.isPaused,
					status: "pending",
					variables: e.variables,
					submittedAt: Date.now()
				};
				case "success": return {
					...t,
					data: e.data,
					failureCount: 0,
					failureReason: null,
					error: null,
					status: "success",
					isPaused: !1
				};
				case "error": return {
					...t,
					data: void 0,
					error: e.error,
					failureCount: t.failureCount + 1,
					failureReason: e.error,
					isPaused: !1,
					status: "error"
				};
			}
		};
		this.state = t(this.state), O.batch(() => {
			this.#t.forEach((t) => {
				t.onMutationUpdate(e);
			}), this.#n.notify({
				mutation: this,
				type: "updated",
				action: e
			});
		});
	}
};
function ct() {
	return {
		context: void 0,
		data: void 0,
		error: null,
		failureCount: 0,
		failureReason: null,
		isPaused: !1,
		status: "idle",
		variables: void 0,
		submittedAt: 0
	};
}
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/mutationCache.js
var lt = class extends Re {
	#e;
	#t;
	#n;
	constructor(e = {}) {
		super(), this.config = e, this.#e = /* @__PURE__ */ new Set(), this.#t = /* @__PURE__ */ new Map(), this.#n = 0;
	}
	build(e, t, n) {
		let r = new st({
			client: e,
			mutationCache: this,
			mutationId: ++this.#n,
			options: e.defaultMutationOptions(t),
			state: n
		});
		return this.add(r), r;
	}
	add(e) {
		this.#e.add(e);
		let t = ut(e);
		if (typeof t == "string") {
			let n = this.#t.get(t);
			n ? n.push(e) : this.#t.set(t, [e]);
		}
		this.notify({
			type: "added",
			mutation: e
		});
	}
	remove(e) {
		if (this.#e.delete(e)) {
			let t = ut(e);
			if (typeof t == "string") {
				let n = this.#t.get(t);
				if (n) {
					if (n.length > 1) {
						let t = n.indexOf(e);
						t !== -1 && n.splice(t, 1);
					} else n[0] === e && this.#t.delete(t);
				}
			}
		}
		this.notify({
			type: "removed",
			mutation: e
		});
	}
	canRun(e) {
		let t = ut(e);
		if (typeof t == "string") {
			let n = this.#t.get(t)?.find((e) => e.state.status === "pending");
			return !n || n === e;
		}
		return !0;
	}
	runNext(e) {
		let t = ut(e);
		return typeof t == "string" ? (this.#t.get(t)?.find((t) => t !== e && t.state.isPaused))?.continue() ?? Promise.resolve() : Promise.resolve();
	}
	clear() {
		O.batch(() => {
			let e = this.getAll();
			this.#e.clear(), this.#t.clear(), e.forEach((e) => {
				this.notify({
					type: "removed",
					mutation: e
				});
			});
		});
	}
	getAll() {
		return Array.from(this.#e);
	}
	find(e) {
		let t = {
			exact: !0,
			...e
		};
		return this.getAll().find((e) => ve(t, e));
	}
	findAll(e = {}) {
		return this.getAll().filter((t) => ve(e, t));
	}
	notify(e) {
		O.batch(() => {
			this.listeners.forEach((t) => {
				t(e);
			});
		});
	}
	resumePausedMutations() {
		let e = this.getAll().filter((e) => e.state.isPaused);
		return O.batch(() => Promise.all(e.map((e) => e.continue().catch(E))));
	}
};
function ut(e) {
	return e.options.scope?.id;
}
// node_modules/.pnpm/@tanstack+query-core@5.104.5/node_modules/@tanstack/query-core/build/modern/mutationObserver.js
var dt = class extends Re {
	#e;
	#t = void 0;
	#n;
	#r;
	constructor(e, t) {
		super(), this.#e = e, this.setOptions(t), this.bindMethods(), this.#i();
	}
	bindMethods() {
		this.mutate = this.mutate.bind(this), this.reset = this.reset.bind(this);
	}
	setOptions(e) {
		let t = this.options;
		this.options = this.#e.defaultMutationOptions(e), we(this.options, t) || this.#e.getMutationCache().notify({
			type: "observerOptionsUpdated",
			mutation: this.#n,
			observer: this
		}), t?.mutationKey && this.options.mutationKey && be(t.mutationKey) !== be(this.options.mutationKey) ? this.reset() : this.#n?.state.status === "pending" && this.#n.setOptions(this.options);
	}
	onSubscribe() {
		this.listeners.size === 1 && this.#n && (this.#n.addObserver(this), this.#i());
	}
	onUnsubscribe() {
		this.hasListeners() || this.#n?.removeObserver(this);
	}
	onMutationUpdate(e) {
		this.#i(), this.#a(e);
	}
	getCurrentResult() {
		return this.#t;
	}
	reset() {
		this.#n?.removeObserver(this), this.#n = void 0, this.#i(), this.#a();
	}
	mutate(e, t) {
		return this.#r = t, this.#n?.removeObserver(this), this.#n = this.#e.getMutationCache().build(this.#e, this.options), this.#n.addObserver(this), this.#n.execute(e);
	}
	#i() {
		let e = this.#n?.state ?? ct();
		this.#t = {
			...e,
			isPending: e.status === "pending",
			isSuccess: e.status === "success",
			isError: e.status === "error",
			isIdle: e.status === "idle",
			mutate: this.mutate,
			reset: this.reset
		};
	}
	#a(e) {
		O.batch(() => {
			let t = this.#r;
			if (t && this.hasListeners()) {
				let n = this.#t.variables, r = this.#t.context, i = {
					client: this.#e,
					meta: this.options.meta,
					mutationKey: this.options.mutationKey
				};
				if (e?.type === "success") {
					try {
						t.onSuccess?.(e.data, n, r, i);
					} catch (e) {
						Promise.reject(e);
					}
					try {
						t.onSettled?.(e.data, null, n, r, i);
					} catch (e) {
						Promise.reject(e);
					}
				} else if (e?.type === "error") {
					try {
						t.onError?.(e.error, n, r, i);
					} catch (e) {
						Promise.reject(e);
					}
					try {
						t.onSettled?.(void 0, e.error, n, r, i);
					} catch (e) {
						Promise.reject(e);
					}
				}
			}
			this.listeners.forEach((e) => {
				e(this.#t);
			});
		});
	}
}, ft = class extends Re {
	#e;
	constructor(e = {}) {
		super(), this.config = e, this.#e = /* @__PURE__ */ new Map();
	}
	build(e, t, n) {
		let r = t.queryKey, i = t.queryHash ?? ye(r, t), a = this.get(i);
		return a || (a = new Ze({
			client: e,
			queryKey: r,
			queryHash: i,
			options: e.defaultQueryOptions(t),
			state: n,
			defaultOptions: e.getQueryDefaults(r)
		}), this.add(a)), a;
	}
	add(e) {
		this.#e.has(e.queryHash) || (this.#e.set(e.queryHash, e), this.notify({
			type: "added",
			query: e
		}));
	}
	remove(e) {
		this.#e.get(e.queryHash) === e && (e.destroy(), this.#e.delete(e.queryHash), this.notify({
			type: "removed",
			query: e
		}));
	}
	clear() {
		O.batch(() => {
			this.getAll().forEach((e) => {
				this.remove(e);
			});
		});
	}
	get(e) {
		return this.#e.get(e);
	}
	getAll() {
		return [...this.#e.values()];
	}
	find(e) {
		let t = {
			exact: !0,
			...e
		};
		return this.getAll().find((e) => _e(t, e));
	}
	findAll(e = {}) {
		let t = this.getAll();
		return Object.keys(e).length > 0 ? t.filter((t) => _e(e, t)) : t;
	}
	notify(e) {
		O.batch(() => {
			this.listeners.forEach((t) => {
				t(e);
			});
		});
	}
	onFocus() {
		O.batch(() => {
			this.getAll().forEach((e) => {
				e.onFocus();
			});
		});
	}
	onOnline() {
		O.batch(() => {
			this.getAll().forEach((e) => {
				e.onOnline();
			});
		});
	}
}, pt = class {
	#e;
	#t;
	#n;
	#r;
	#i;
	#a;
	#o;
	#s;
	constructor(e = {}) {
		this.#e = e.queryCache || new ft(), this.#t = e.mutationCache || new lt(), this.#n = e.defaultOptions || {}, this.#r = /* @__PURE__ */ new Map(), this.#i = /* @__PURE__ */ new Map(), this.#a = 0;
	}
	mount() {
		this.#a++, this.#a === 1 && (this.#o = ze.subscribe(async (e) => {
			e && (await this.resumePausedMutations(), this.#e.onFocus());
		}), this.#s = He.subscribe(async (e) => {
			e && (await this.resumePausedMutations(), this.#e.onOnline());
		}));
	}
	unmount() {
		this.#a--, this.#a === 0 && (this.#o?.(), this.#o = void 0, this.#s?.(), this.#s = void 0);
	}
	isFetching(e) {
		return this.#e.findAll({
			...e,
			fetchStatus: "fetching"
		}).length;
	}
	isMutating(e) {
		return this.#t.findAll({
			...e,
			status: "pending"
		}).length;
	}
	getQueryData(e) {
		let t = this.defaultQueryOptions({ queryKey: e });
		return this.#e.get(t.queryHash)?.state.data;
	}
	ensureQueryData(e) {
		let t = this.defaultQueryOptions(e), n = this.#e.build(this, t), r = n.state.data;
		return r === void 0 ? this.fetchQuery(e) : (e.revalidateIfStale && n.isStaleByTime(D(t.staleTime, n)) && this.prefetchQuery(t), Promise.resolve(r));
	}
	getQueriesData(e) {
		return this.#e.findAll(e).map(({ queryKey: e, state: t }) => [e, t.data]);
	}
	setQueryData(e, t, n) {
		let r = this.defaultQueryOptions({ queryKey: e }), i = this.#e.get(r.queryHash)?.state.data, a = me(t, i);
		if (a !== void 0) return this.#e.build(this, r).setData(a, {
			...n,
			manual: !0
		});
	}
	setQueriesData(e, t, n) {
		return O.batch(() => this.#e.findAll(e).map(({ queryKey: e }) => [e, this.setQueryData(e, t, n)]));
	}
	getQueryState(e) {
		let t = this.defaultQueryOptions({ queryKey: e });
		return this.#e.get(t.queryHash)?.state;
	}
	removeQueries(e) {
		let t = this.#e;
		O.batch(() => {
			t.findAll(e).forEach((e) => {
				t.remove(e);
			});
		});
	}
	resetQueries(e, t) {
		let n = this.#e;
		return O.batch(() => {
			let r = n.findAll(e), i = new Set(r);
			return r.forEach((e) => {
				e.reset();
			}), this.refetchQueries({
				type: "active",
				predicate: (e) => i.has(e)
			}, t);
		});
	}
	cancelQueries(e, t = {}) {
		let n = {
			revert: !0,
			...t
		}, r = O.batch(() => this.#e.findAll(e).map((e) => e.cancel(n)));
		return Promise.all(r).then(E).catch(E);
	}
	invalidateQueries(e, t = {}) {
		return O.batch(() => (this.#e.findAll(e).forEach((e) => {
			e.invalidate();
		}), e?.refetchType === "none" ? Promise.resolve() : this.refetchQueries({
			...e,
			type: e?.refetchType ?? e?.type ?? "active"
		}, t)));
	}
	refetchQueries(e, t = {}) {
		let n = {
			...t,
			cancelRefetch: t.cancelRefetch ?? !0
		}, r = O.batch(() => this.#e.findAll(e).filter((e) => !e.isDisabled() && !e.isStatic()).map((e) => {
			let t = e.fetch(void 0, n);
			return n.throwOnError || (t = t.catch(E)), e.state.fetchStatus === "paused" ? Promise.resolve() : t;
		}));
		return Promise.all(r).then(E);
	}
	async query(e) {
		let t = this.defaultQueryOptions(e);
		t.retry === void 0 && (t.retry = !1);
		let n = this.#e.build(this, t), r = n.isStaleByTime(D(t.staleTime, n)) ? await n.fetch(t) : n.state.data, i = t.select;
		return i ? i(r) : r;
	}
	fetchQuery(e) {
		let t = this.defaultQueryOptions(e);
		t.retry === void 0 && (t.retry = !1);
		let n = this.#e.build(this, t);
		return n.isStaleByTime(D(t.staleTime, n)) ? n.fetch(t) : Promise.resolve(n.state.data);
	}
	prefetchQuery(e) {
		return this.fetchQuery(e).then(E).catch(E);
	}
	infiniteQuery(e) {
		return e._type = "infinite", this.query(e);
	}
	fetchInfiniteQuery(e) {
		return e._type = "infinite", this.fetchQuery(e);
	}
	prefetchInfiniteQuery(e) {
		return this.fetchInfiniteQuery(e).then(E).catch(E);
	}
	ensureInfiniteQueryData(e) {
		return e._type = "infinite", this.ensureQueryData(e);
	}
	resumePausedMutations() {
		return He.isOnline() ? this.#t.resumePausedMutations() : Promise.resolve();
	}
	getQueryCache() {
		return this.#e;
	}
	getMutationCache() {
		return this.#t;
	}
	getDefaultOptions() {
		return this.#n;
	}
	setDefaultOptions(e) {
		this.#n = e;
	}
	setQueryDefaults(e, t) {
		this.#r.set(be(e), {
			queryKey: e,
			defaultOptions: t
		});
	}
	getQueryDefaults(e) {
		let t = [...this.#r.values()], n = {};
		return t.forEach((t) => {
			xe(e, t.queryKey) && Object.assign(n, t.defaultOptions);
		}), n;
	}
	setMutationDefaults(e, t) {
		this.#i.set(be(e), {
			mutationKey: e,
			defaultOptions: t
		});
	}
	getMutationDefaults(e) {
		let t = [...this.#i.values()], n = {};
		return t.forEach((t) => {
			xe(e, t.mutationKey) && Object.assign(n, t.defaultOptions);
		}), n;
	}
	defaultQueryOptions(e) {
		if (e._defaulted) return e;
		let t = {
			...this.#n.queries,
			...this.getQueryDefaults(e.queryKey),
			...e,
			_defaulted: !0
		};
		return t.queryHash ||= ye(t.queryKey, t), t.refetchOnReconnect === void 0 && (t.refetchOnReconnect = t.networkMode !== "always"), t.throwOnError === void 0 && (t.throwOnError = !!t.suspense), !t.networkMode && t.persister && (t.networkMode = "offlineFirst"), t.queryFn === Me && (t.enabled = !1), t;
	}
	defaultMutationOptions(e) {
		return e?._defaulted ? e : {
			...this.#n.mutations,
			...e?.mutationKey && this.getMutationDefaults(e.mutationKey),
			...e,
			_defaulted: !0
		};
	}
	clear() {
		this.#e.clear(), this.#t.clear();
	}
}, mt = S.createContext(!1), ht = () => S.useContext(mt);
mt.Provider;
// node_modules/.pnpm/@tanstack+react-query@5.104.5_@types+react@19.3.0_react@19.3.1/node_modules/@tanstack/react-query/build/modern/QueryErrorResetBoundary.js
function gt() {
	let e = !1;
	return {
		clearReset: () => {
			e = !1;
		},
		reset: () => {
			e = !0;
		},
		isReset: () => e
	};
}
var _t = S.createContext(gt()), vt = () => S.useContext(_t), yt = (e, t, n) => {
	let r = n?.state.status === "error" && typeof e.throwOnError == "function" ? Pe(e.throwOnError, [n.state.error, n]) : e.throwOnError;
	(e.suspense || r) && (t.isReset() || (e.retryOnMount = !1));
}, bt = (e) => {
	S.useEffect(() => {
		e.clearReset();
	}, [e]);
}, xt = ({ result: e, errorResetBoundary: t, throwOnError: n, query: r, suspense: i }) => e.isError && !t.isReset() && !e.isFetching && r && (i && e.data === void 0 || Pe(n, [e.error, r])), St = (e) => {
	if (e.suspense) {
		let t = 1e3, n = (e) => e === "static" ? e : Math.max(e ?? t, t), r = e.staleTime;
		e.staleTime = typeof r == "function" ? (...e) => n(r(...e)) : n(r), typeof e.gcTime == "number" && (e.gcTime = Math.max(e.gcTime, t));
	}
}, Ct = (e, t) => e?.suspense && t.isPending, wt = (e, t, n) => t.fetchOptimistic(e).catch(() => {
	n.clearReset();
});
// node_modules/.pnpm/@tanstack+react-query@5.104.5_@types+react@19.3.0_react@19.3.1/node_modules/@tanstack/react-query/build/modern/useBaseQuery.js
function Tt(e, t, n) {
	let r = ht(), i = vt(), a = ce(n), o = a.defaultQueryOptions(e), s = a.getQueryCache().get(o.queryHash), c = e.subscribed !== !1;
	o._optimisticResults = r ? "isRestoring" : c ? "optimistic" : void 0, St(o), yt(o, i, s), bt(i);
	let [l] = S.useState(() => new t(a, o)), u = l.getOptimisticResult(o), d = !r && c;
	if (S.useSyncExternalStore(S.useCallback((e) => {
		let t = d ? l.subscribe(O.batchCalls(e)) : E;
		return l.updateResult(), t;
	}, [l, d]), () => l.getCurrentResult(), () => l.getCurrentResult()), S.useEffect(() => {
		l.setOptions(o);
	}, [o, l]), Ct(o, u)) throw wt(o, l, i);
	if (xt({
		result: u,
		errorResetBoundary: i,
		throwOnError: o.throwOnError,
		query: s,
		suspense: o.suspense
	})) throw u.error;
	return o.notifyOnChangeProps ? u : l.trackResult(u);
}
// node_modules/.pnpm/@tanstack+react-query@5.104.5_@types+react@19.3.0_react@19.3.1/node_modules/@tanstack/react-query/build/modern/useQuery.js
function Et(e, t) {
	return Tt(e, tt, t);
}
// node_modules/.pnpm/@tanstack+react-query@5.104.5_@types+react@19.3.0_react@19.3.1/node_modules/@tanstack/react-query/build/modern/useMutation.js
function Dt(e, t) {
	let n = ce(t), [r] = S.useState(() => new dt(n, e));
	S.useEffect(() => {
		r.setOptions(e);
	}, [r, e]);
	let i = S.useSyncExternalStore(S.useCallback((e) => r.subscribe(O.batchCalls(e)), [r]), () => r.getCurrentResult(), () => r.getCurrentResult()), a = S.useCallback((...e) => {
		r.mutate(e[0], e[1]).catch(E);
	}, [r]);
	if (i.error && Pe(r.options.throwOnError, [i.error])) throw i.error;
	return {
		...i,
		mutate: a,
		mutateAsync: i.mutate
	};
}
// studio-src/format.ts
function Ot(e) {
	let t = new Date(e);
	return Number.isNaN(t.getTime()) ? e : t.toLocaleString();
}
// studio-src/EntryCodesTable.tsx
function kt(e) {
	return e === null ? "Never" : Ot(e);
}
function At({ codes: t, onRevoke: n }) {
	return t.length === 0 ? /* @__PURE__ */ w(o, {
		title: "No entry codes",
		description: "Mint a code to hand out — anyone who opens its chat link enters the gated route."
	}) : /* @__PURE__ */ w(l, {
		label: "Entry codes",
		children: /* @__PURE__ */ T(_, { children: [/* @__PURE__ */ w(h, { children: /* @__PURE__ */ T(g, { children: [
			/* @__PURE__ */ w(m, { children: "Label" }),
			/* @__PURE__ */ w(m, { children: "Created" }),
			/* @__PURE__ */ w(m, { children: "Expires" }),
			/* @__PURE__ */ w(m, { "aria-label": "Actions" })
		] }) }), /* @__PURE__ */ w(f, { children: t.map((t) => /* @__PURE__ */ T(g, { children: [
			/* @__PURE__ */ w(p, { children: t.label ?? "—" }),
			/* @__PURE__ */ w(p, { children: Ot(t.created_at) }),
			/* @__PURE__ */ w(p, { children: kt(t.expires_at) }),
			/* @__PURE__ */ w(p, {
				style: { textAlign: "right" },
				children: /* @__PURE__ */ w(e, {
					variant: "ghost",
					"aria-label": `Revoke entry code ${t.label ?? t.code_id}`,
					onClick: () => {
						n(t.code_id);
					},
					children: "Revoke"
				})
			})
		] }, t.code_id)) })] })
	});
}
// studio-src/compose-chat-url.ts
function jt(e, t, n, r) {
	return `${(e === "" ? window.location.origin : e).replace(/\/+$/, "")}${t}/chat/${encodeURIComponent(n)}?tai_entry=${encodeURIComponent(r)}`;
}
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/core/util.js
function Mt(e) {
	let t = Object.values(e).filter((e) => typeof e == "number");
	return Object.entries(e).filter(([e, n]) => t.indexOf(+e) === -1).map(([e, t]) => t);
}
function Nt(e, t = "|") {
	return e.map((e) => Qt(e)).join(t);
}
function Pt(e, t) {
	return typeof t == "bigint" ? t.toString() : t;
}
var Ft = class {
	constructor(e) {
		this._getter = e, this._value = void 0;
	}
	get value() {
		let e = this._getter;
		return e !== void 0 && (this._value = e(), this._getter = void 0), this._value;
	}
};
function It(e) {
	return new Ft(e);
}
function Lt(e) {
	return e == null;
}
function Rt(e) {
	let t = +!!e.startsWith("^"), n = e.endsWith("$") ? e.length - 1 : e.length;
	return e.slice(t, n);
}
function k(e, t, n) {
	Object.defineProperty(e, t, {
		value: n,
		writable: !0,
		enumerable: !0,
		configurable: !0
	});
}
function zt(e) {
	let t = Object.getOwnPropertyDescriptor(e, "shape");
	return t?.get ? t.get.raw : t?.value;
}
function A(e) {
	return zt(e._zod.def) ?? e._zod.def.shape;
}
function Bt(e, t, n) {
	Object.defineProperty(e, t, {
		get() {
			let e = n();
			return k(this, t, e), e;
		},
		enumerable: !0,
		configurable: !0
	});
}
function Vt(e, t, n) {
	t in e ? k(e, t, n) : e[t] = n;
}
function j(e, t, n, r) {
	let i = A(t);
	for (let a of n) {
		let n = Object.getOwnPropertyDescriptor(i, a);
		n.enumerable && (n.get ? Bt(e, a, () => {
			let e = t._zod.def.shape[a];
			return r ? r(e, a) : e;
		}) : Vt(e, a, r ? r(n.value, a) : n.value));
	}
}
function Ht(e, t) {
	for (let n of Reflect.ownKeys(t)) {
		let r = Object.getOwnPropertyDescriptor(t, n);
		r.enumerable && (r.get ? Bt(e, n, () => t[n]) : Vt(e, n, r.value));
	}
}
function M(...e) {
	let t = {};
	for (let n of e) {
		let e = Object.getOwnPropertyDescriptors(n);
		Object.assign(t, e);
	}
	return Object.defineProperties({}, t);
}
function Ut(e) {
	return JSON.stringify(e);
}
function Wt(e) {
	return e.toLowerCase().trim().replace(/[^\w\s-]/g, "").replace(/[\s_-]+/g, "-").replace(/^-+|-+$/g, "");
}
var Gt = "captureStackTrace" in Error ? Error.captureStackTrace : (...e) => {};
function Kt(e) {
	return typeof e == "object" && !!e && !Array.isArray(e);
}
var qt = /* @__PURE__*/ It(() => {
	if (V.jitless || typeof navigator < "u" && navigator?.userAgent?.includes("Cloudflare")) return !1;
	try {
		return Function(""), !0;
	} catch {
		return !1;
	}
});
function Jt(e) {
	if (Kt(e) === !1) return !1;
	let t = e.constructor;
	if (t === void 0 || typeof t != "function") return !0;
	let n = t.prototype;
	return Kt(n) !== !1 && Object.prototype.hasOwnProperty.call(n, "isPrototypeOf") !== !1;
}
function Yt(e) {
	return Jt(e) ? { ...e } : Array.isArray(e) ? [...e] : e instanceof Map ? new Map(e) : e instanceof Set ? new Set(e) : e;
}
var Xt = /* @__PURE__*/ new Set([
	"string",
	"number",
	"symbol"
]);
function Zt(e) {
	return e.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function N(e, t, n) {
	let r = new e._zod.constr(t ?? e._zod.def);
	return (!t || n?.parent) && (r._zod.parent = e), r;
}
function P(e) {
	let t = e;
	if (!t) return {};
	if (typeof t == "string") return { error: () => t };
	if (t?.message !== void 0) {
		if (t?.error !== void 0) throw Error("Cannot specify both `message` and `error` params");
		t.error = t.message;
	}
	return delete t.message, typeof t.error == "string" ? {
		...t,
		error: () => t.error
	} : t;
}
function Qt(e) {
	return typeof e == "bigint" ? e.toString() + "n" : typeof e == "string" ? `"${e}"` : `${e}`;
}
function $t(e) {
	return Object.keys(e).filter((t) => e[t]._zod.optin !== void 0 && e[t]._zod.optout === "optional");
}
var en = {
	safeint: [-(2 ** 53 - 1), 2 ** 53 - 1],
	int32: [-2147483648, 2147483647],
	uint32: [0, 4294967295],
	float32: [-34028234663852886e22, 34028234663852886e22],
	float64: [-Number.MAX_VALUE, Number.MAX_VALUE]
}, tn = {
	int64: [/* @__PURE__*/ BigInt("-9223372036854775808"), /* @__PURE__*/ BigInt("9223372036854775807")],
	uint64: [/* @__PURE__*/ BigInt(0), /* @__PURE__*/ BigInt("18446744073709551615")]
};
function nn(e, t) {
	let n = e._zod.def, r = n.checks;
	if (r && r.length > 0) throw Error(".pick() cannot be used on object schemas containing refinements");
	let i = {};
	return j(i, e, rn(e, t)), N(e, M(n, {
		shape: i,
		checks: []
	}));
}
function rn(e, t) {
	let n = A(e), r = [];
	for (let e of Reflect.ownKeys(t)) {
		if (!Object.getOwnPropertyDescriptor(n, e)?.enumerable) throw Error(`Unrecognized key: "${String(e)}"`);
		t[e] && r.push(e);
	}
	return r;
}
function an(e, t) {
	let n = e._zod.def, r = n.checks;
	if (r && r.length > 0) throw Error(".omit() cannot be used on object schemas containing refinements");
	let i = new Set(rn(e, t)), a = {};
	return j(a, e, Reflect.ownKeys(A(e)).filter((e) => !i.has(e))), N(e, M(n, {
		shape: a,
		checks: []
	}));
}
function on(e, t) {
	if (!Jt(t)) throw Error("Invalid input to extend: expected a plain object");
	let n = e._zod.def.checks;
	if (n && n.length > 0) {
		let n = A(e);
		for (let e of Reflect.ownKeys(t)) if (Object.getOwnPropertyDescriptor(n, e) !== void 0) throw Error("Cannot overwrite keys on object schemas containing refinements. Use `.safeExtend()` instead.");
	}
	return N(e, M(e._zod.def, { shape: sn(e, t) }));
}
function sn(e, t) {
	let n = {};
	return j(n, e, Reflect.ownKeys(A(e))), Ht(n, t), n;
}
function cn(e, t) {
	if (!Jt(t)) throw Error("Invalid input to safeExtend: expected a plain object");
	return N(e, M(e._zod.def, { shape: sn(e, t) }));
}
function ln(e, t) {
	if (!t?._zod?.def) throw Error("Invalid input to merge: expected an object schema. To merge a plain shape, use `.extend()`.");
	if (e._zod.def.checks?.length) throw Error(".merge() cannot be used on object schemas containing refinements. Use .safeExtend() instead.");
	let n = {};
	return j(n, e, Reflect.ownKeys(A(e))), j(n, t, Reflect.ownKeys(A(t))), N(e, M(e._zod.def, {
		shape: n,
		get catchall() {
			return t._zod.def.catchall;
		},
		checks: t._zod.def.checks ?? []
	}));
}
function un(e, t, n, r = "partial") {
	let i = t._zod.def.checks;
	if (i && i.length > 0) throw Error(`.${r}() cannot be used on object schemas containing refinements`);
	let a = n ? new Set(rn(t, n)) : void 0, o = {};
	return j(o, t, Reflect.ownKeys(A(t)), e && ((t, n) => a && !a.has(n) ? t : new e({
		type: "optional",
		innerType: t
	}))), N(t, M(t._zod.def, {
		shape: o,
		checks: []
	}));
}
function dn(e, t, n) {
	let r = n ? new Set(rn(t, n)) : void 0, i = {};
	return j(i, t, Reflect.ownKeys(A(t)), (t, n) => r && !r.has(n) ? t : new e({
		type: "nonoptional",
		innerType: t
	})), N(t, M(t._zod.def, { shape: i }));
}
function F(e, t = 0) {
	if (e.aborted === !0) return !0;
	for (let n = t; n < e.issues.length; n++) if (e.issues[n]?.continue !== !0) return !0;
	return !1;
}
function fn(e, t = 0) {
	if (e.aborted === !0) return !0;
	for (let n = t; n < e.issues.length; n++) if (e.issues[n]?.continue === !1) return !0;
	return !1;
}
function pn(e, t) {
	return t.map((t) => {
		var n;
		return (n = t).path ?? (n.path = []), t.path.unshift(e), t;
	});
}
function mn(e) {
	return typeof e == "string" ? e : e?.message;
}
function hn(e) {
	let t = [];
	for (let n of e ?? []) {
		let e = n._zod.def;
		if (!/_(length|size)$|_equals$/.test(e.check)) break;
		let r = e.maximum ?? e.length ?? e.size;
		r >= 0 && t.push([r, n]);
	}
	return t;
}
function gn(e, t, n) {
	var r;
	for (let i = t; i < e.length; i++) (r = e[i]).schema ?? (r.schema = n);
}
function I(e, t, n) {
	var r;
	let i = e.inst?._zod?.traits;
	i?.has("$ZodType") && (i.has("$ZodCheck") ? (r = e).schema ?? (r.schema = e.inst) : e.schema = e.inst);
	let a = e.schema === e.inst ? void 0 : e.schema?._zod.def?.error, o = e.message ? e.message : mn(e.inst?._zod.def?.error?.(e)) ?? mn(a?.(e)) ?? mn(t?.error?.(e)) ?? mn(n.customError?.(e)) ?? mn(n.localeError?.(e)) ?? "Invalid input", s = {};
	for (let t of Object.keys(e)) t !== "inst" && t !== "schema" && t !== "continue" && t !== "input" && t !== "__proto__" && (s[t] = e[t]);
	return s.path ??= [], s.message = o, t?.reportInput && (s.input = e.input), s;
}
var _n = /[\uD800-\uDBFF]/;
function vn(e) {
	let t = e.length;
	if (!_n.test(e)) return t;
	let n = t;
	for (let r = 0; r < t - 1; r++) (e.charCodeAt(r) & 64512) == 55296 && (e.charCodeAt(r + 1) & 64512) == 56320 && (n--, r++);
	return n;
}
function yn(e) {
	return Array.isArray(e) ? "array" : typeof e == "string" ? "string" : "unknown";
}
function bn(e) {
	let t = typeof e;
	switch (t) {
		case "number": return Number.isNaN(e) ? "nan" : "number";
		case "object": {
			if (e === null) return "null";
			if (Array.isArray(e)) return "array";
			let t = e;
			if (t && Object.getPrototypeOf(t) !== Object.prototype && "constructor" in t && t.constructor) return t.constructor.name;
		}
	}
	return t;
}
function xn(...e) {
	let [t, n, r] = e;
	return typeof t == "string" ? {
		message: t,
		code: "custom",
		input: n,
		inst: r
	} : { ...t };
}
function Sn(e, t) {
	for (let n in t) {
		let r = Object.getOwnPropertyDescriptor(t, n);
		r.get ? Object.defineProperty(e, n, {
			...r,
			enumerable: !1
		}) : Tn(e, n, r.value);
	}
}
function L(e, t, n, r = !0) {
	return Object.defineProperty(e, t, {
		configurable: !0,
		writable: !0,
		enumerable: r,
		value: n
	}), n;
}
function Cn(e, t, n) {
	return L(e, t, n, !1);
}
/* @__NO_SIDE_EFFECTS__ */
function wn(e, t) {
	for (let n in e) {
		let r = e[n];
		Object.defineProperty(t, n, {
			configurable: !0,
			enumerable: !0,
			get() {
				return L(this, n, r(this));
			},
			set(e) {
				L(this, n, e);
			}
		});
	}
	return t;
}
function Tn(e, t, n) {
	Object.defineProperty(e, t, {
		configurable: !0,
		get() {
			return this == null ? n : L(this, t, n.bind(this));
		},
		set(e) {
			L(this, t, e);
		}
	});
}
function En(e, t) {
	let n = Object.getPrototypeOf(e);
	return t in n ? void 0 : n;
}
var Dn, On = !1, kn = {
	configurable: !0,
	get() {
		On = !0;
	}
};
function R(e, t, n) {
	let r = Object.getPrototypeOf(e._zod);
	t in r && Dn !== e._zod ? Dn = void 0 : (Dn = e._zod, Object.defineProperty(r, t, {
		configurable: !0,
		get() {
			Object.defineProperty(this, t, kn);
			let e = On;
			On = !1;
			try {
				let r = n(this);
				return On ? delete this[t] : Object.defineProperty(this, t, {
					configurable: !0,
					writable: !0,
					value: r
				}), On ||= e, r;
			} catch (n) {
				throw delete this[t], On ||= e, n;
			}
		},
		set(e) {
			Object.defineProperty(this, t, {
				configurable: !0,
				writable: !0,
				value: e
			});
		}
	}));
}
function An(e, t, n, r) {
	let i = En(e, t);
	i && Object.defineProperty(i, t, {
		configurable: !0,
		get() {
			let e = {
				configurable: !0,
				writable: !0,
				enumerable: r,
				value: void 0
			};
			return Object.defineProperty(this, t, e), e.value = n(this), Object.defineProperty(this, t, e), e.value;
		},
		set(e) {
			Object.defineProperty(this, t, {
				configurable: !0,
				writable: !0,
				enumerable: r,
				value: e
			});
		}
	});
}
var jn = "~constantCatch";
function Mn(e) {
	let t = () => e;
	return t[jn] = !0, t;
}
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/core/core.js
var Nn, Pn = {
	value: void 0,
	enumerable: !1
}, Fn = "captureStackTrace" in Error ? Error : null;
function In(e) {
	let t = Fn;
	if (t) {
		let n = t.stackTraceLimit;
		if (typeof n == "number") {
			try {
				t.stackTraceLimit = 0;
			} catch {
				return Fn = null, new e();
			}
			try {
				return new e();
			} finally {
				t.stackTraceLimit = n;
			}
		}
	}
	return new e();
}
/* @__NO_SIDE_EFFECTS__ */
function z(e, t, n, r) {
	let i = {};
	function a(e) {
		this.def = e, this.constr = d, this.traits = /* @__PURE__ */ new Set();
	}
	a.prototype = i;
	let o = n, s = o && /* @__PURE__ */ new WeakSet();
	function c(n, r) {
		if (!n._zod) {
			Pn.value = new a(r);
			try {
				Object.defineProperty(n, "_zod", Pn);
			} finally {
				Pn.value = void 0;
			}
		} else if (n._zod.traits.has(e)) return;
		if (n._zod.traits.add(e), t(n, r), s) {
			let e = Object.getPrototypeOf(n), t = n._zod.constr.prototype, r = e;
			for (; r && r !== t;) r = Object.getPrototypeOf(r);
			let i = r ?? e;
			s.has(i) || (s.add(i), Sn(i, o));
		}
		let i = d.prototype;
		for (let e in i) Object.prototype.hasOwnProperty.call(i, e) && (e in n || (n[e] = i[e].bind(n)));
	}
	let l = r?.Parent ?? Object;
	class u extends l {}
	Object.defineProperty(u, "name", { value: e });
	function d(e) {
		let t = r?.Parent ? In(u) : this;
		c(t, e);
		let n = t._zod.deferred;
		if (n) {
			for (let e of n) e();
			t._zod.deferred = void 0;
		}
		let i = globalThis.__zod_globalConfig?.postProcessor;
		return i && i(t), t;
	}
	return Object.defineProperty(d, "init", { value: c }), Object.defineProperty(d, Symbol.hasInstance, { value: (t) => r?.Parent && t instanceof r.Parent ? !0 : t?._zod?.traits?.has(e) }), Object.defineProperty(d, "name", { value: e }), d;
}
var B = class extends Error {
	constructor() {
		super("Encountered Promise during synchronous parse. Use .parseAsync() instead.");
	}
}, Ln = class extends Error {
	constructor(e) {
		super(`Encountered unidirectional transform during encode: ${e}`), this.name = "ZodEncodeError";
	}
};
(Nn = globalThis).__zod_globalConfig ?? (Nn.__zod_globalConfig = {});
var V = globalThis.__zod_globalConfig;
function H(e) {
	return e && Object.assign(V, e), V;
}
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/core/errors.js
function Rn() {
	let e = this._zod;
	return e.message ??= JSON.stringify(e.def, Pt, 2), e.message;
}
function zn(e) {
	this._zod.message = e;
}
var Bn = {
	get: Rn,
	set: zn,
	enumerable: !0,
	configurable: !0
}, Vn = {
	value: void 0,
	enumerable: !1
}, Hn = /* @__PURE__ */ new WeakSet([Object.prototype, Error.prototype]), Un = /* @__PURE__ */ z("$ZodError", (e, t) => {
	e.name = "$ZodError", Vn.value = t, Object.defineProperty(e, "issues", Vn), Vn.value = void 0, Object.defineProperty(e, "message", Bn);
	let n = Object.getPrototypeOf(e);
	Hn.has(n) || (Hn.add(n), Object.defineProperty(n, "toString", {
		configurable: !0,
		enumerable: !1,
		get() {
			let e = () => this.message;
			return Object.defineProperty(this, "toString", {
				value: e,
				configurable: !0,
				writable: !0
			}), e;
		},
		set(e) {
			Object.defineProperty(this, "toString", {
				value: e,
				configurable: !0,
				writable: !0
			});
		}
	}));
});
function Wn(e, t, n) {
	return Object.prototype.hasOwnProperty.call(e, t) || (t === "__proto__" ? Object.defineProperty(e, t, {
		value: n(),
		writable: !0,
		enumerable: !0,
		configurable: !0
	}) : e[t] = n()), e[t];
}
function Gn(e, t = (e) => e.message) {
	let n = {}, r = [];
	for (let i of e.issues) i.path.length > 0 ? Wn(n, i.path[0], () => []).push(t(i)) : r.push(t(i));
	return {
		formErrors: r,
		fieldErrors: n
	};
}
function Kn(e, t = (e) => e.message) {
	let n = { _errors: [] }, r = (e, i = []) => {
		for (let a of e.issues) if (a.code === "invalid_union" && a.errors.length) a.errors.map((e) => r({ issues: e }, [...i, ...a.path]));
		else if (a.code === "invalid_key") r({ issues: a.issues }, [...i, ...a.path]);
		else if (a.code === "invalid_element") r({ issues: a.issues }, [...i, ...a.path]);
		else {
			let e = [...i, ...a.path];
			if (e.length === 0) n._errors.push(t(a));
			else {
				let r = n, i = 0;
				for (; i < e.length;) {
					let n = e[i], o = i === e.length - 1;
					if (n === "_errors") {
						o && r._errors.push(t(a)), i++;
						continue;
					}
					Object.prototype.hasOwnProperty.call(r, n) || Object.defineProperty(r, n, {
						value: { _errors: [] },
						enumerable: !0,
						writable: !0,
						configurable: !0
					});
					let s = r[n];
					o && s._errors.push(t(a)), r = s, i++;
				}
			}
		}
	};
	return r(e), n;
}
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/core/parse.js
function qn(e, t) {
	return {
		callee: t?.callee ?? e,
		Err: t?.Err
	};
}
var Jn = (e) => {
	let t = (n, r, i, a) => {
		let o = i ? {
			...i,
			async: !1
		} : { async: !1 }, s = n._zod.run({
			value: r,
			issues: []
		}, o);
		if (s instanceof Promise) throw new B();
		if (s.issues.length) {
			let n = new ((a?.Err) ?? e)(s.issues.map((e) => I(e, o, H())));
			throw Gt(n, a?.callee ?? t), n;
		}
		return s.value;
	};
	return t;
}, Yn = (e) => {
	let t = async (n, r, i, a) => {
		let o = i ? {
			...i,
			async: !0
		} : { async: !0 }, s = n._zod.run({
			value: r,
			issues: []
		}, o);
		if (s instanceof Promise && (s = await s), s.issues.length) {
			let n = new ((a?.Err) ?? e)(s.issues.map((e) => I(e, o, H())));
			throw Gt(n, a?.callee ?? t), n;
		}
		return s.value;
	};
	return t;
}, Xn = (e) => (t, n, r) => {
	let i = r ? {
		...r,
		async: !1
	} : { async: !1 }, a = t._zod.run({
		value: n,
		issues: []
	}, i);
	if (a instanceof Promise) throw new B();
	return a.issues.length ? Zn(e, a.issues, i) : {
		success: !0,
		data: a.value
	};
};
function Zn(e, t, n) {
	let r;
	return {
		success: !1,
		get error() {
			return r || (r = new e(t.map((e) => I(e, n, H()))), t = void 0, n = void 0), r;
		},
		set error(e) {
			r = e, t = void 0, n = void 0;
		}
	};
}
var Qn = (e) => async (t, n, r) => {
	let i = r ? {
		...r,
		async: !0
	} : { async: !0 }, a = t._zod.run({
		value: n,
		issues: []
	}, i);
	return a instanceof Promise && (a = await a), a.issues.length ? Zn(e, a.issues, i) : {
		success: !0,
		data: a.value
	};
}, $n = /* @__PURE__ */ Symbol.for("zod.compile.invalid"), er = /* @__PURE__ */ Symbol.for("zod.compile.fallback"), tr = ((e, t, n) => {
	let r = e._zod.bag.validator;
	if (r !== void 0) {
		if (r(t) !== $n) return !0;
		if (r.definite === !0 && n === void 0) return !1;
	}
	return nr(e, t, n);
});
function nr(e, t, n) {
	let r = n ? {
		...n,
		async: !1,
		abortEarly: !0
	} : {
		async: !1,
		abortEarly: !0
	}, i = e._zod.bag.fallbackRun, a;
	if (i ? (r[er] = !0, a = i({
		value: t,
		issues: []
	}, r)) : a = e._zod.run({
		value: t,
		issues: []
	}, r), a instanceof Promise) throw new B();
	return a.issues.length === 0;
}
var rr = async (e, t, n) => {
	let r = n ? {
		...n,
		async: !0,
		abortEarly: !0
	} : {
		async: !0,
		abortEarly: !0
	}, i = e._zod.run({
		value: t,
		issues: []
	}, r);
	return i instanceof Promise && (i = await i), i.issues.length === 0;
}, ir = (e) => {
	let t = Jn(e), n = (e, r, i, a) => {
		let o = i ? {
			...i,
			direction: "backward"
		} : { direction: "backward" };
		return t(e, r, o, qn(n, a));
	};
	return n;
}, ar = (e) => {
	let t = Jn(e), n = (e, r, i, a) => t(e, r, i, qn(n, a));
	return n;
}, or = (e) => {
	let t = Yn(e), n = async (e, r, i, a) => {
		let o = i ? {
			...i,
			direction: "backward"
		} : { direction: "backward" };
		return await t(e, r, o, qn(n, a));
	};
	return n;
}, sr = (e) => {
	let t = Yn(e), n = async (e, r, i, a) => await t(e, r, i, qn(n, a));
	return n;
}, cr = (e) => (t, n, r) => {
	let i = r ? {
		...r,
		direction: "backward"
	} : { direction: "backward" };
	return Xn(e)(t, n, i);
}, lr = (e) => (t, n, r) => Xn(e)(t, n, r), ur = (e) => async (t, n, r) => {
	let i = r ? {
		...r,
		direction: "backward"
	} : { direction: "backward" };
	return Qn(e)(t, n, i);
}, dr = (e) => async (t, n, r) => Qn(e)(t, n, r), fr = /^[cC][0-9a-z]{6,}$/, pr = /^[0-9a-z]+$/, mr = /^[0-7][0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{25}$/, hr = /^[0-9a-vA-V]{20}$/, gr = /^[A-Za-z0-9]{27}$/, _r = /^[a-zA-Z0-9_-]{21}$/;
function vr(e) {
	return RegExp(`^[a-zA-Z0-9_-]{${e}}$`);
}
var yr = /^P(?:(\d+W)|(?!.*W)(?=\d|T\d)(\d+Y)?(\d+M)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+([.,]\d+)?S)?)?)$/, br = /^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$/, xr = (e) => e ? RegExp(`^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-${e}[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$`) : /^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$/, Sr = /^(?:[A-Za-z0-9_'+\-]+\.)*[A-Za-z0-9_'+\-]*[A-Za-z0-9_+-]@(?:[A-Za-z0-9][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}$/, Cr = "^(?=[\\s\\S]*[\\p{Extended_Pictographic}\\p{Regional_Indicator}\\u20E3])[\\p{Extended_Pictographic}\\p{Emoji_Component}]+$";
function wr() {
	return new RegExp(Cr, "u");
}
var Tr = /^(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])$/, Er = /^(([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:))$/, Dr = /^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])\/([0-9]|[1-2][0-9]|3[0-2])$/, Or = /^(([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:))\/(12[0-8]|1[01][0-9]|[1-9]?[0-9])$/, kr = /^$|^(?:[0-9a-zA-Z+/]{4})*(?:(?:[0-9a-zA-Z+/]{2}==)|(?:[0-9a-zA-Z+/]{3}=))?$/, Ar = /^(?:[A-Za-z0-9_-]{4})*(?:[A-Za-z0-9_-]{2,3})?$/, jr = /^https?$/, Mr = /^\+[1-9]\d{6,14}$/, Nr = "(?:(?:\\d\\d[2468][048]|\\d\\d[13579][26]|\\d\\d0[48]|[02468][048]00|[13579][26]00)-02-29|\\d{4}-(?:(?:0[13578]|1[02])-(?:0[1-9]|[12]\\d|3[01])|(?:0[469]|11)-(?:0[1-9]|[12]\\d|30)|(?:02)-(?:0[1-9]|1\\d|2[0-8])))";
function Pr(e) {
	return RegExp(`^${e}$`);
}
var Fr = /*@__PURE__*/ Pr(Nr);
function Ir(e) {
	let t = "(?:[01]\\d|2[0-3]):[0-5]\\d";
	return typeof e.precision == "number" ? e.precision === -1 ? `${t}` : e.precision === 0 ? `${t}:[0-5]\\d` : `${t}:[0-5]\\d\\.\\d{${e.precision}}` : e.seconds ? `${t}:[0-5]\\d(?:\\.\\d+)?` : `${t}(?::[0-5]\\d(?:\\.\\d+)?)?`;
}
function Lr(e) {
	return RegExp(`^${Ir(e)}$`);
}
function Rr(e) {
	let t = ["Z"];
	e.offset && t.push("([+-](?:[01]\\d|2[0-3]):[0-5]\\d)");
	let n = `${Ir({
		precision: e.precision,
		seconds: !0
	})}(?:${t.join("|")})`, r = e.local ? `${n}|${Ir({ precision: e.precision })}` : n;
	return RegExp(`^${Nr}T(?:${r})$`);
}
var zr = /^[\s\S]{0,}$/, Br = /^-?\d+$/, Vr = /^-?\d+(?:\.\d+)?$/, Hr = /^(?:true|false)$/i, Ur = /^[^A-Z]*$/, Wr = /^[^a-z]*$/, U = /*@__PURE__*/ z("$ZodCheck", (e, t) => {
	var n;
	e._zod ??= {}, e._zod.def = t, (n = e._zod).onattach ?? (n.onattach = []);
}), Gr = (e) => {
	let t = e.value;
	return !Lt(t) && t.length !== void 0;
}, Kr = /*@__PURE__*/ z("$ZodCheckMaxLength", (e, t) => {
	var n;
	U.init(e, t), (n = e._zod.def).when ?? (n.when = Gr), e._zod.check = (n) => {
		let r = n.value, i = r.length;
		if ((typeof r == "string" && i > t.maximum ? vn(r) : i) <= t.maximum) return;
		let a = yn(r);
		n.issues.push({
			origin: a,
			code: "too_big",
			maximum: t.maximum,
			inclusive: !0,
			input: r,
			inst: e,
			continue: !t.abort
		});
	};
}), qr = /*@__PURE__*/ z("$ZodCheckMinLength", (e, t) => {
	var n;
	U.init(e, t), (n = e._zod.def).when ?? (n.when = Gr), e._zod.check = (n) => {
		let r = n.value, i = r.length;
		if ((typeof r == "string" && i >= t.minimum && i < t.minimum * 2 ? vn(r) : i) >= t.minimum) return;
		let a = yn(r);
		n.issues.push({
			origin: a,
			code: "too_small",
			minimum: t.minimum,
			inclusive: !0,
			input: r,
			inst: e,
			continue: !t.abort
		});
	};
}), Jr = /*@__PURE__*/ z("$ZodCheckLengthEquals", (e, t) => {
	var n;
	U.init(e, t), (n = e._zod.def).when ?? (n.when = Gr), e._zod.check = (n) => {
		let r = n.value, i = r.length, a = typeof r == "string" && i >= t.length && i <= t.length * 2 ? vn(r) : i;
		if (a === t.length) return;
		let o = yn(r), s = a > t.length;
		n.issues.push({
			origin: o,
			...s ? {
				code: "too_big",
				maximum: t.length
			} : {
				code: "too_small",
				minimum: t.length
			},
			inclusive: !0,
			exact: !0,
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), Yr = /*@__PURE__*/ z("$ZodCheckStringFormat", (e, t) => {
	var n, r;
	U.init(e, t), t.pattern ? (n = e._zod).check ?? (n.check = (n) => {
		t.pattern.lastIndex = 0, !t.pattern.test(n.value) && n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: t.format,
			input: n.value,
			...t.pattern ? { pattern: t.pattern.toString() } : {},
			inst: e,
			continue: !t.abort
		});
	}) : (r = e._zod).check ?? (r.check = () => {});
}), Xr = /*@__PURE__*/ z("$ZodCheckRegex", (e, t) => {
	Yr.init(e, t), e._zod.check = (n) => {
		t.pattern.lastIndex = 0, !t.pattern.test(n.value) && n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: "regex",
			input: n.value,
			pattern: t.pattern.toString(),
			inst: e,
			continue: !t.abort
		});
	};
}), Zr = /*@__PURE__*/ z("$ZodCheckLowerCase", (e, t) => {
	t.pattern ??= Ur, Yr.init(e, t);
}), Qr = /*@__PURE__*/ z("$ZodCheckUpperCase", (e, t) => {
	t.pattern ??= Wr, Yr.init(e, t);
}), $r = /*@__PURE__*/ z("$ZodCheckIncludes", (e, t) => {
	U.init(e, t);
	let n = Zt(t.includes);
	t.pattern = new RegExp(typeof t.position == "number" ? `^.{${t.position},}${n}` : n), e._zod.check = (n) => {
		n.value.includes(t.includes, t.position) || n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: "includes",
			includes: t.includes,
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), ei = /*@__PURE__*/ z("$ZodCheckStartsWith", (e, t) => {
	U.init(e, t);
	let n = RegExp(`^${Zt(t.prefix)}.*`);
	t.pattern ??= n, e._zod.check = (n) => {
		n.value.startsWith(t.prefix) || n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: "starts_with",
			prefix: t.prefix,
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), ti = /*@__PURE__*/ z("$ZodCheckEndsWith", (e, t) => {
	U.init(e, t);
	let n = RegExp(`.*${Zt(t.suffix)}$`);
	t.pattern ??= n, e._zod.check = (n) => {
		n.value.endsWith(t.suffix) || n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: "ends_with",
			suffix: t.suffix,
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), ni = /*@__PURE__*/ z("$ZodCheckOverwrite", (e, t) => {
	U.init(e, t), e._zod.check = (e) => {
		e.value = t.tx(e.value);
	};
}), ri = class {
	constructor(e = [], t = {}) {
		this.content = [], this.indent = 0, this.args = e, this.closed = t;
	}
	indented(e) {
		this.indent += 1;
		try {
			e(this);
		} finally {
			--this.indent;
		}
	}
	write(e) {
		if (typeof e == "function") {
			e(this, { execution: "sync" }), e(this, { execution: "async" });
			return;
		}
		let t = e.split("\n").filter((e) => e), n = Math.min(...t.map((e) => e.length - e.trimStart().length)), r = t.map((e) => e.slice(n)).map((e) => " ".repeat(this.indent * 2) + e);
		for (let e of r) this.content.push(e);
	}
	compile() {
		let e = Function, t = this?.content ?? [""];
		return new e(...Object.keys(this.closed), `return function (${this.args.join(", ")}) {\n${t.join("\n")}\n};`)(...Object.values(this.closed));
	}
}, ii = {
	major: 4,
	minor: 6,
	patch: 7
}, W = /*@__PURE__*/ z("$ZodType", (e, t) => {
	var n;
	e ??= {}, e._zod.def = t, e._zod.bag = e._zod.bag || {}, e._zod.version = ii;
	let r = e._zod.def.checks, i = e._zod.traits.has("$ZodCheck") ? [e, ...r ?? []] : r?.length ? [...r] : [];
	for (let t of i) for (let n of t._zod.onattach) n(e);
	if (i.length === 0) (n = e._zod).deferred ?? (n.deferred = []), e._zod.deferred?.push(() => {
		e._zod.run = e._zod.parse;
	});
	else {
		let t = (t, n, r) => {
			if (t.memo) return t;
			let i = F(t), a;
			for (let o of n) {
				if (o._zod.def.when) {
					if (fn(t) || !o._zod.def.when(t)) continue;
				} else if (i) continue;
				let n = t.issues.length, s = o._zod.check(t);
				if (s instanceof Promise && r?.async === !1) throw new B();
				if (a || s instanceof Promise) a = (a ?? Promise.resolve()).then(async () => {
					await s, t.issues.length !== n && (gn(t.issues, n, e), i ||= F(t, n));
				});
				else {
					if (t.issues.length === n) continue;
					gn(t.issues, n, e), i ||= F(t, n);
				}
			}
			return a ? a.then(() => t) : t;
		}, n = (n, r, a) => {
			if (F(n)) return n.aborted = !0, n;
			let o = t(r, i, a);
			if (o instanceof Promise) {
				if (a.async === !1) throw new B();
				return o.then((t) => e._zod.parse(t, a));
			}
			return e._zod.parse(o, a);
		};
		e._zod.run = (r, a) => {
			if (a.skipChecks) return e._zod.parse(r, a);
			if (a.direction === "backward") {
				let t = e._zod.parse({
					value: r.value,
					issues: []
				}, {
					...a,
					skipChecks: !0
				});
				return t instanceof Promise ? t.then((e) => n(e, r, a)) : n(t, r, a);
			}
			let o = e._zod.parse(r, a);
			if (o instanceof Promise) {
				if (a.async === !1) throw new B();
				return o.then((e) => t(e, i, a));
			}
			return t(o, i, a);
		};
	}
}, {
	get "~standard"() {
		return Cn(this, "~standard", si(this));
	},
	set "~standard"(e) {
		L(this, "~standard", e);
	}
}), ai = (e, t) => e.issues.length ? { issues: e.issues.map((e) => I(e, t, H())) } : { value: e.value };
async function oi(e, t) {
	let n = { async: !0 };
	return ai(await e._zod.run({
		value: t,
		issues: []
	}, n), n);
}
function si(e) {
	return {
		validate: (t) => {
			let n = { async: !1 };
			try {
				let r = e._zod.run({
					value: t,
					issues: []
				}, n);
				if (!(r instanceof Promise)) return ai(r, n);
			} catch {}
			return oi(e, t);
		},
		vendor: "zod",
		version: 1
	};
}
var ci = /*@__PURE__*/ z("$ZodString", (e, t) => {
	W.init(e, t), e._zod.pattern = t.pattern ?? zr, e._zod.parse = (n, r) => {
		if (t.coerce) try {
			n.value = String(n.value);
		} catch {}
		return typeof n.value == "string" || n.issues.push({
			expected: "string",
			code: "invalid_type",
			input: n.value,
			inst: e
		}), n;
	};
}), G = /*@__PURE__*/ z("$ZodStringFormat", (e, t) => {
	Yr.init(e, t), ci.init(e, t);
}), li = /*@__PURE__*/ z("$ZodGUID", (e, t) => {
	t.pattern ??= br, G.init(e, t);
}), ui = /*@__PURE__*/ z("$ZodUUID", (e, t) => {
	if (t.version) {
		let e = {
			v1: 1,
			v2: 2,
			v3: 3,
			v4: 4,
			v5: 5,
			v6: 6,
			v7: 7,
			v8: 8
		}[t.version];
		if (e === void 0) throw Error(`Invalid UUID version: "${t.version}"`);
		t.pattern ??= xr(e);
	} else t.pattern ??= xr();
	G.init(e, t);
}), di = /*@__PURE__*/ z("$ZodEmail", (e, t) => {
	t.pattern ??= Sr, G.init(e, t);
});
function fi(e) {
	try {
		return typeof URL < "u" && typeof URL.canParse == "function" ? URL.canParse(e) : (new URL(e), !0);
	} catch {
		return !1;
	}
}
function pi(e, t) {
	return !("normalize" in t) && !("hostname" in t) && !("protocol" in t) ? fi(e) || 2 : mi(e, t);
}
function mi(e, t) {
	if (!t.normalize && t.protocol?.source === jr.source && !/^https?:\/\//i.test(e)) return 1;
	try {
		if (typeof URL < "u") {
			let t = URL;
			if (typeof t.parse == "function") return t.parse(e) ?? 2;
		}
		return new URL(e);
	} catch {
		return 2;
	}
}
var hi = /[\t\n\r]/g;
function gi(e) {
	return e.replace(hi, "");
}
function _i(e, t) {
	return t.lastIndex = 0, t.test(e.hostname);
}
function vi(e, t) {
	return t.lastIndex = 0, t.test(e.protocol.endsWith(":") ? e.protocol.slice(0, -1) : e.protocol);
}
var yi = /*@__PURE__*/ z("$ZodURL", (e, t) => {
	G.init(e, t), e._zod.check = (n) => {
		try {
			let r = n.value.trim(), i = pi(r, t);
			if (i === 1) {
				n.issues.push({
					code: "invalid_format",
					format: "url",
					note: "Invalid URL format",
					input: n.value,
					inst: e,
					continue: !t.abort
				});
				return;
			}
			if (i === 2) {
				n.issues.push({
					code: "invalid_format",
					format: "url",
					input: n.value,
					inst: e,
					continue: !t.abort
				});
				return;
			}
			if (i === !0) {
				n.value = gi(r);
				return;
			}
			t.hostname && !_i(i, t.hostname) && n.issues.push({
				code: "invalid_format",
				format: "url",
				note: "Invalid hostname",
				pattern: t.hostname.source,
				input: n.value,
				inst: e,
				continue: !t.abort
			}), t.protocol && !vi(i, t.protocol) && n.issues.push({
				code: "invalid_format",
				format: "url",
				note: "Invalid protocol",
				pattern: t.protocol.source,
				input: n.value,
				inst: e,
				continue: !t.abort
			}), n.value = t.normalize ? i.href : gi(r);
			return;
		} catch {
			n.issues.push({
				code: "invalid_format",
				format: "url",
				input: n.value,
				inst: e,
				continue: !t.abort
			});
		}
	};
}), bi = /*@__PURE__*/ z("$ZodEmoji", (e, t) => {
	t.pattern ??= wr(), G.init(e, t);
}), xi = /*@__PURE__*/ z("$ZodNanoID", (e, t) => {
	if (t.length !== void 0 && (!Number.isInteger(t.length) || t.length < 1)) throw Error(`Invalid nanoid length: ${t.length}`);
	t.pattern ??= t.length === void 0 ? _r : vr(t.length), G.init(e, t);
}), Si = /*@__PURE__*/ z("$ZodCUID", (e, t) => {
	t.pattern ??= fr, G.init(e, t);
}), Ci = /*@__PURE__*/ z("$ZodCUID2", (e, t) => {
	t.pattern ??= pr, G.init(e, t);
}), wi = /*@__PURE__*/ z("$ZodULID", (e, t) => {
	t.pattern ??= mr, G.init(e, t);
}), Ti = /*@__PURE__*/ z("$ZodXID", (e, t) => {
	t.pattern ??= hr, G.init(e, t);
}), Ei = /*@__PURE__*/ z("$ZodKSUID", (e, t) => {
	t.pattern ??= gr, G.init(e, t);
}), Di = /*@__PURE__*/ z("$ZodISODateTime", (e, t) => {
	t.pattern ??= Rr(t), G.init(e, t);
}), Oi = /*@__PURE__*/ z("$ZodISODate", (e, t) => {
	t.pattern ??= Fr, G.init(e, t);
}), ki = /*@__PURE__*/ z("$ZodISOTime", (e, t) => {
	t.pattern ??= Lr(t), G.init(e, t);
}), Ai = /*@__PURE__*/ z("$ZodISODuration", (e, t) => {
	t.pattern ??= yr, G.init(e, t);
}), ji = /*@__PURE__*/ z("$ZodIPv4", (e, t) => {
	t.pattern ??= Tr, G.init(e, t);
}), Mi = /^[0-9a-fA-F:.]+$/;
function Ni(e) {
	return Mi.test(e) ? fi(`http://[${e}]`) : !1;
}
var Pi = /*@__PURE__*/ z("$ZodIPv6", (e, t) => {
	t.pattern ??= Er, G.init(e, t), e._zod.check = (n) => {
		Ni(n.value) || n.issues.push({
			code: "invalid_format",
			format: "ipv6",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), Fi = /*@__PURE__*/ z("$ZodCIDRv4", (e, t) => {
	t.pattern ??= Dr, G.init(e, t);
});
function Ii(e) {
	let t = e.split("/");
	if (t.length !== 2) return !1;
	let [n, r] = t;
	if (!r) return !1;
	let i = Number(r);
	return `${i}` !== r || i < 0 || i > 128 ? !1 : Ni(n);
}
var Li = /*@__PURE__*/ z("$ZodCIDRv6", (e, t) => {
	t.pattern ??= Or, G.init(e, t), e._zod.check = (n) => {
		Ii(n.value) || n.issues.push({
			code: "invalid_format",
			format: "cidrv6",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
});
function Ri(e) {
	if (e === "") return !0;
	if (/\s/.test(e) || e.length % 4 != 0) return !1;
	try {
		return atob(e), !0;
	} catch {
		return !1;
	}
}
var zi = /^[0-9a-zA-Z+/]*={0,2}$/, Bi = /*@__PURE__*/ z("$ZodBase64", (e, t) => {
	t.pattern ??= zi, G.init(e, t), e._zod.check = (n) => {
		Ri(n.value) || n.issues.push({
			code: "invalid_format",
			format: "base64",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), Vi = /^[A-Za-z0-9_-]*$/;
function Hi(e) {
	if (!Vi.test(e)) return !1;
	let t = e.replace(/[-_]/g, (e) => e === "-" ? "+" : "/");
	return Ri(t.padEnd(Math.ceil(t.length / 4) * 4, "="));
}
var Ui = /*@__PURE__*/ z("$ZodBase64URL", (e, t) => {
	t.pattern ??= Vi, G.init(e, t), e._zod.check = (n) => {
		Hi(n.value) || n.issues.push({
			code: "invalid_format",
			format: "base64url",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), Wi = /*@__PURE__*/ z("$ZodE164", (e, t) => {
	t.pattern ??= Mr, G.init(e, t);
});
function Gi(e, t = null) {
	try {
		let n = e.split(".");
		if (n.length !== 3) return !1;
		let [r] = n;
		if (!r) return !1;
		let i = JSON.parse(atob(r));
		return !("typ" in i && i?.typ !== "JWT" || !i.alg || t && (!("alg" in i) || i.alg !== t));
	} catch {
		return !1;
	}
}
var Ki = /*@__PURE__*/ z("$ZodJWT", (e, t) => {
	G.init(e, t), e._zod.check = (n) => {
		Gi(n.value, t.alg) || n.issues.push({
			code: "invalid_format",
			format: "jwt",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), qi = /*@__PURE__*/ z("$ZodBoolean", (e, t) => {
	W.init(e, t), e._zod.pattern = Hr, e._zod.parse = (n, r) => {
		if (t.coerce) try {
			n.value = !!n.value;
		} catch {}
		let i = n.value;
		return typeof i == "boolean" || n.issues.push({
			expected: "boolean",
			code: "invalid_type",
			input: i,
			inst: e
		}), n;
	};
}), Ji = /*@__PURE__*/ z("$ZodUnknown", (e, t) => {
	W.init(e, t), e._zod.parse = (e) => e;
}), Yi = /*@__PURE__*/ z("$ZodNever", (e, t) => {
	W.init(e, t), e._zod.parse = (t, n) => (t.issues.push({
		expected: "never",
		code: "invalid_type",
		input: t.value,
		inst: e
	}), t);
});
function Xi(e, t) {
	let n = hn(e._zod.def.checks);
	if (!n.length) return;
	let r = Math.min(...n.map((e) => e[0])), i = e._zod.parse;
	e._zod.parse = (n, a) => {
		let o = t(n.value);
		if (o > r && !a?.skipChecks && a?.direction !== "backward") {
			for (let [t, r] of hn(e._zod.def.checks)) if (o > t && r._zod.def.when(n)) {
				let t = n.issues.length;
				return r._zod.check(n), n.issues[t].continue = !1, gn(n.issues, t, e), n;
			}
		}
		return i(n, a);
	};
}
var Zi = (e) => Array.isArray(e) ? e.length : -1;
function Qi(e, t, n) {
	e.issues.length && t.issues.push(...pn(n, e.issues)), t.value[n] = e.value;
}
var $i = /*@__PURE__*/ z("$ZodArray", (e, t) => {
	W.init(e, t);
	let n = V.memoizer;
	n?.attach(e), e._zod.parse = (r, i) => {
		let a = r.value;
		if (!Array.isArray(a)) return r.issues.push({
			expected: "array",
			code: "invalid_type",
			input: a,
			inst: e
		}), r;
		r.value = n ? n.alloc(e, r, Array(a.length), i) : Array(a.length);
		let o = [], s = i?.abortEarly;
		for (let e = 0; e < a.length; e++) {
			let n = a[e], c = t.element._zod.run({
				value: n,
				issues: []
			}, i);
			if (c instanceof Promise) o.push(c.then((t) => Qi(t, r, e)));
			else if (Qi(c, r, e), s && c.issues.length !== 0 && F(c)) break;
		}
		return o.length ? Promise.all(o).then(() => r) : r;
	}, Xi(e, Zi);
});
function ea(e, t, n, r, i, a) {
	let o = n in r, s = a === "optional";
	if (o || !s || i !== "optional") {
		if (e.issues.length) {
			if (i !== void 0 && s && !o) return;
			t.issues.push(...pn(n, e.issues));
		}
		!o && i === void 0 ? e.issues.length || t.issues.push({
			code: "invalid_type",
			expected: "nonoptional",
			input: void 0,
			path: [n]
		}) : e.value === void 0 ? (o || i === "defaulted" && !s) && (t.value[n] = void 0) : t.value[n] = e.value;
	}
}
var ta = [];
function na(e) {
	let t = Object.keys(e.shape), n = Object.getOwnPropertySymbols(e.shape), r = n.length ? n : ta, i = r.length ? [...t, ...r] : t;
	for (let t of i) if (!e.shape?.[t]?._zod?.traits?.has("$ZodType")) throw Error(`Invalid element at key "${String(t)}": expected a Zod schema`);
	let a = $t(e.shape);
	return {
		...e,
		allKeys: i,
		symbolKeys: r,
		keySet: new Set(t),
		numKeys: t.length,
		optionalKeys: new Set(a)
	};
}
function ra(e, t, n, r, i, a, o) {
	let s = [], c = i.keySet, l = i.catchall._zod, u = l.def.type, d = l.optin, f = l.optout, p = 0;
	for (let i in t) {
		if (o && n.issues.length !== p) {
			if (F(n, p)) break;
			p = n.issues.length;
		}
		if (c.has(i)) continue;
		if (i === "__proto__") {
			u === "never" && s.push(i);
			continue;
		}
		if (u === "never") {
			s.push(i);
			continue;
		}
		let a = l.run({
			value: t[i],
			issues: []
		}, r);
		a instanceof Promise ? e.push(a.then((e) => ea(e, n, i, t, d, f))) : ea(a, n, i, t, d, f);
	}
	return s.length && n.issues.push({
		code: "unrecognized_keys",
		keys: s,
		input: t,
		inst: a,
		continue: !0
	}), e.length ? Promise.all(e).then(() => n) : n;
}
var ia = /*@__PURE__*/ z("$ZodObject", (e, t) => {
	W.init(e, t);
	let n = Object.getOwnPropertyDescriptor(t, "shape"), r = n?.get ? n.get.raw : t.shape ?? {};
	if (r) {
		let e = () => {
			let n = { ...r };
			return Object.defineProperty(t, "shape", { value: n }), e.raw = n, n;
		};
		e.raw = r, Object.defineProperty(t, "shape", { get: e });
	}
	let i = It(() => na(t));
	R(e, "propValues", (e) => {
		let t = e.def.shape, n = {};
		for (let e in t) {
			let r = t[e]._zod;
			if (r.values) {
				Object.prototype.hasOwnProperty.call(n, e) || k(n, e, /* @__PURE__ */ new Set());
				for (let t of r.values) n[e].add(t);
				r.optin !== void 0 && n[e].add(void 0);
			}
		}
		return n;
	});
	let a = Kt, o = t.catchall, s, c = V.memoizer;
	c?.attach(e), e._zod.parse = (t, n) => {
		s ??= i.value;
		let r = t.value;
		if (!a(r)) return t.issues.push({
			expected: "object",
			code: "invalid_type",
			input: r,
			inst: e
		}), t;
		t.value = c ? c.alloc(e, t, {}, n) : {};
		let l = [], u = s.shape, d = n?.abortEarly, f = t.issues.length;
		for (let e of s.allKeys) {
			if (d && t.issues.length !== f) {
				if (F(t, f)) break;
				f = t.issues.length;
			}
			if (e === "__proto__") continue;
			let i = u[e], a = i._zod.optin, o = i._zod.optout, s = i._zod.run({
				value: r[e],
				issues: []
			}, n);
			s instanceof Promise ? l.push(s.then((n) => ea(n, t, e, r, a, o))) : ea(s, t, e, r, a, o);
		}
		return o ? ra(l, r, t, n, i.value, e, d === !0) : l.length ? Promise.all(l).then(() => t) : t;
	};
}), aa = /*@__PURE__*/ z("$ZodObjectJIT", (e, t) => {
	ia.init(e, t);
	let n = e._zod.parse, r = It(() => na(t)), i = V.memoizer, a = (t) => {
		let n = r.value, a = n.symbolKeys, o = new ri(["payload", "ctx"], {
			shape: t,
			inst: e,
			memo: i,
			syms: a
		}), s = (e) => `shape[${e}]._zod.run({ value: input[${e}], issues: [] }, ctx)`, c = (e, t) => `
          let ${e}_ab = false;
          for (let i = 0; i < ${e}.issues.length; i++) {
            const iss = ${e}.issues[i];
            iss.path = iss.path ? [${t}, ...iss.path] : [${t}];
            payload.issues.push(iss);
            if (iss.continue !== true) ${e}_ab = true;
          }
          if (${e}_ab && ctx && ctx.abortEarly) {
            payload.value = newResult;
            return payload;
          }`;
		o.write("const input = payload.value;");
		let l = Object.create(null), u = 0;
		for (let e of n.allKeys) l[e] = `key_${u++}`;
		o.write(i ? "const newResult = memo.alloc(inst, payload, {}, ctx);" : "const newResult = {};");
		for (let e of n.allKeys) {
			if (e === "__proto__") continue;
			let n = l[e], r = typeof e == "symbol" ? `syms[${a.indexOf(e)}]` : Ut(e), i = `${r} in input`, u = t[e], d = u?._zod?.optin, f = d !== void 0, p = u?._zod?.optout === "optional";
			if (o.write(`const ${n} = ${s(r)};`), f && p) {
				let e = d === "optional" ? `${n}_present` : `${n}.value !== undefined || ${n}_present`;
				o.write(`
        const ${n}_present = ${i};
        if (!${n}.issues.length || ${n}_present) {
          if (${n}.issues.length) {${c(n, r)}
          }

          if (${e}) {
            newResult[${r}] = ${n}.value;
          }
        }

      `);
			} else f ? (o.write(`
        if (${n}.issues.length) {${c(n, r)}
        }
      `), d === "defaulted" ? o.write(`newResult[${r}] = ${n}.value;`) : o.write(`
        if (${n}.value !== undefined || ${i}) {
          newResult[${r}] = ${n}.value;
        }
      `)) : o.write(`
        const ${n}_present = ${i};
        if (${n}.issues.length) {${c(n, r)}
        }
        if (!${n}_present && !${n}.issues.length) {
          payload.issues.push({
            code: "invalid_type",
            expected: "nonoptional",
            input: undefined,
            path: [${r}]
          });
          if (ctx && ctx.abortEarly) {
            payload.value = newResult;
            return payload;
          }
        }

        if (${n}_present) {
          newResult[${r}] = ${n}.value;
        }

      `);
		}
		return o.write("payload.value = newResult;"), o.write("return payload;"), o.compile();
	}, o, s = Kt, c = !V.jitless, l = c && qt.value, u = t.catchall, d;
	e._zod.parse = (i, f) => {
		d ??= r.value;
		let p = i.value;
		return s(p) ? c && l && f?.async === !1 && f.jitless !== !0 ? (o ||= a(t.shape), i = o(i, f), u ? ra([], p, i, f, d, e, f?.abortEarly === !0) : i) : n(i, f) : (i.issues.push({
			expected: "object",
			code: "invalid_type",
			input: p,
			inst: e
		}), i);
	};
});
function oa(e, t, n, r) {
	for (let n of e) if (n.issues.length === 0) return t.value = n.value, t;
	let i = e.filter((e) => !F(e));
	return i.length === 1 ? (t.value = i[0].value, i[0]) : (t.issues.push({
		code: "invalid_union",
		input: t.value,
		inst: n,
		errors: e.map((e) => e.issues.map((e) => I(e, r, H())))
	}), t);
}
var sa = /*@__PURE__*/ z("$ZodUnion", (e, t) => {
	W.init(e, t), R(e, "optin", (e) => e.def.options.some((e) => e._zod.optin === "defaulted") ? "defaulted" : e.def.options.some((e) => e._zod.optin !== void 0) ? "optional" : void 0), R(e, "optout", (e) => e.def.options.some((e) => e._zod.optout === "optional") ? "optional" : void 0), R(e, "values", (e) => {
		if (e.def.options.every((e) => e._zod.values)) return new Set(e.def.options.flatMap((e) => Array.from(e._zod.values)));
	}), R(e, "pattern", (e) => {
		if (e.def.options.every((e) => e._zod.pattern)) {
			let t = e.def.options.map((e) => e._zod.pattern);
			return RegExp(`^(${t.map((e) => Rt(e.source)).join("|")})$`);
		}
	});
	let n = t.options.length === 1 ? t.options[0]._zod.run : null;
	e._zod.parse = (r, i) => {
		if (n) return n(r, i);
		let a = !1, o = [];
		for (let e of t.options) {
			let t = e._zod.run({
				value: r.value,
				issues: []
			}, i);
			if (t instanceof Promise) o.push(t), a = !0;
			else {
				if (t.issues.length === 0) return t;
				o.push(t);
			}
		}
		return a ? Promise.all(o).then((t) => oa(t, r, e, i)) : oa(o, r, e, i);
	};
}), ca = /*@__PURE__*/ z("$ZodIntersection", (e, t) => {
	W.init(e, t), e._zod.parse = (e, n) => {
		let r = e.value, i = t.left._zod.run({
			value: r,
			issues: []
		}, n), a = t.right._zod.run({
			value: r,
			issues: []
		}, n);
		return i instanceof Promise || a instanceof Promise ? Promise.all([i, a]).then(([t, n]) => ua(e, t, n)) : ua(e, i, a);
	};
});
function la(e, t) {
	if (e === t || e instanceof Date && t instanceof Date && +e == +t) return {
		valid: !0,
		data: e
	};
	if (Jt(e) && Jt(t)) {
		let n = Object.keys(t), r = Object.keys(e).filter((e) => n.indexOf(e) !== -1), i = {
			...e,
			...t
		};
		Object.prototype.hasOwnProperty.call(i, "__proto__") && delete i.__proto__;
		for (let n of r) {
			if (n === "__proto__") continue;
			let r = la(e[n], t[n]);
			if (!r.valid) return {
				valid: !1,
				mergeErrorPath: [n, ...r.mergeErrorPath]
			};
			i[n] = r.data;
		}
		return {
			valid: !0,
			data: i
		};
	}
	if (Array.isArray(e) && Array.isArray(t)) {
		if (e.length !== t.length) return {
			valid: !1,
			mergeErrorPath: []
		};
		let n = [];
		for (let r = 0; r < e.length; r++) {
			let i = e[r], a = t[r], o = la(i, a);
			if (!o.valid) return {
				valid: !1,
				mergeErrorPath: [r, ...o.mergeErrorPath]
			};
			n.push(o.data);
		}
		return {
			valid: !0,
			data: n
		};
	}
	return {
		valid: !1,
		mergeErrorPath: []
	};
}
function ua(e, t, n) {
	let r = /* @__PURE__ */ new Map(), i, a = /* @__PURE__ */ new Map(), o = (e, t) => {
		let n;
		if (e.code === "unrecognized_keys" && !e.path?.length) i ??= e, n = e.keys;
		else if (e.code === "invalid_key" && e.origin === "record" && e.path?.length === 1) {
			let t = String(e.path[0]);
			a.has(t) || a.set(t, e), n = [t];
		} else return !1;
		for (let e of n) r.has(e) || r.set(e, {}), r.get(e)[t] = !0;
		return !0;
	};
	for (let n of t.issues) o(n, "l") || e.issues.push(n);
	for (let t of n.issues) o(t, "r") || e.issues.push(t);
	let s = [...r].filter(([, e]) => e.l && e.r).map(([e]) => e);
	if (s.length) {
		let t = i ? s.filter((e) => i.keys.includes(e)) : [];
		t.length && e.issues.push({
			...i,
			keys: t
		});
		for (let n of s) !t.includes(n) && a.has(n) && e.issues.push(a.get(n));
	}
	let c = la(t.value, n.value);
	if (!c.valid) {
		if (F(e)) return e;
		throw Error(`Unmergable intersection. Error path: ${JSON.stringify(c.mergeErrorPath)}`);
	}
	return e.value = c.data, e;
}
var da = /*@__PURE__*/ z("$ZodRecord", (e, t) => {
	W.init(e, t);
	let n = V.memoizer;
	n?.attach(e), e._zod.parse = (r, i) => {
		let a = r.value;
		if (!Jt(a)) return r.issues.push({
			expected: "record",
			code: "invalid_type",
			input: a,
			inst: e
		}), r;
		let o = [], s = t.keyType._zod.values;
		if (s && !t.partial) {
			r.value = n ? n.alloc(e, r, {}, i) : {};
			let c = /* @__PURE__ */ new Set();
			for (let n of s) if (typeof n == "string" || typeof n == "number" || typeof n == "symbol") {
				if (c.add(typeof n == "number" ? n.toString() : n), n === "__proto__") continue;
				let s = t.keyType._zod.run({
					value: n,
					issues: []
				}, i);
				if (s instanceof Promise) throw Error("Async schemas not supported in object keys currently");
				if (s.issues.length) {
					r.issues.push({
						code: "invalid_key",
						origin: "record",
						issues: s.issues.map((e) => I(e, i, H())),
						input: n,
						path: [n],
						inst: e
					});
					continue;
				}
				let l = s.value;
				if (l === "__proto__") continue;
				let u = t.valueType._zod.run({
					value: a[n],
					issues: []
				}, i);
				u instanceof Promise ? o.push(u.then((e) => {
					e.issues.length && r.issues.push(...pn(n, e.issues)), r.value[l] = e.value;
				})) : (u.issues.length && r.issues.push(...pn(n, u.issues)), r.value[l] = u.value);
			}
			let l;
			for (let e in a) if (!c.has(e)) {
				if (t.mode === "loose") {
					if (e === "__proto__") continue;
					r.value[e] = a[e];
				} else l ??= [], l.push(e);
			}
			l && l.length > 0 && r.issues.push({
				code: "unrecognized_keys",
				input: a,
				inst: e,
				keys: l,
				continue: !0
			});
		} else {
			r.value = n ? n.alloc(e, r, {}, i) : {};
			let c;
			for (let n of Reflect.ownKeys(a)) {
				if (n === "__proto__" || !Object.prototype.propertyIsEnumerable.call(a, n)) continue;
				let l = t.keyType._zod.run({
					value: n,
					issues: []
				}, i);
				if (l instanceof Promise) throw Error("Async schemas not supported in object keys currently");
				if (typeof n == "string" && Vr.test(n) && l.issues.length) {
					let e = t.keyType._zod.run({
						value: Number(n),
						issues: []
					}, i);
					if (e instanceof Promise) throw Error("Async schemas not supported in object keys currently");
					e.issues.length === 0 && (l = e);
				}
				if (l.issues.length) {
					t.mode === "loose" ? r.value[n] = a[n] : s ? (c ??= [], c.push(n)) : r.issues.push({
						code: "invalid_key",
						origin: "record",
						issues: l.issues.map((e) => I(e, i, H())),
						input: n,
						path: [n],
						inst: e
					});
					continue;
				}
				let u = l.value;
				if (u === "__proto__") continue;
				let d = t.valueType._zod.run({
					value: a[n],
					issues: []
				}, i);
				d instanceof Promise ? o.push(d.then((e) => {
					e.issues.length && r.issues.push(...pn(n, e.issues)), r.value[u] = e.value;
				})) : (d.issues.length && r.issues.push(...pn(n, d.issues)), r.value[u] = d.value);
			}
			c && c.length > 0 && r.issues.push({
				code: "unrecognized_keys",
				input: a,
				inst: e,
				keys: c,
				continue: !0
			});
		}
		return o.length ? Promise.all(o).then(() => r) : r;
	};
}), fa = /*@__PURE__*/ z("$ZodEnum", (e, t) => {
	W.init(e, t);
	let n = Mt(t.entries), r = new Set(n);
	e._zod.values = r, R(e, "pattern", (e) => {
		let t = Mt(e.def.entries).filter((e) => Xt.has(typeof e));
		return RegExp(t.length ? `^(${t.map((e) => Zt(e.toString())).join("|")})$` : "^[^\\s\\S]$");
	}), e._zod.parse = (t, i) => {
		let a = t.value;
		return r.has(a) || t.issues.push({
			code: "invalid_value",
			values: n,
			input: a,
			inst: e
		}), t;
	};
}), pa = /*@__PURE__*/ z("$ZodLiteral", (e, t) => {
	W.init(e, t);
	let n = new Set(t.values);
	e._zod.values = n, R(e, "pattern", (e) => {
		let t = e.def.values;
		return RegExp(t.length ? `^(${t.map((e) => typeof e == "string" ? Zt(e) : e ? Zt(e.toString()) : String(e)).join("|")})$` : "^[^\\s\\S]$");
	}), e._zod.parse = (r, i) => {
		let a = r.value;
		return n.has(a) || r.issues.push({
			code: "invalid_value",
			values: t.values,
			input: a,
			inst: e
		}), r;
	};
}), ma = /*@__PURE__*/ z("$ZodTransform", (e, t) => {
	W.init(e, t), e._zod.optin = "optional", V.memoizer?.guard(e), e._zod.parse = (n, r) => {
		if (r.direction === "backward") throw new Ln(e.constructor.name);
		let i = t.transform(n.value, n);
		if (r.async) return (i instanceof Promise ? i : Promise.resolve(i)).then((e) => (n.value = e, n));
		if (i instanceof Promise) throw new B();
		return n.value = i, n;
	};
});
function ha(e, t) {
	return e.value = t.issues.length ? void 0 : t.value, e;
}
var ga = /*@__PURE__*/ z("$ZodOptional", (e, t) => {
	W.init(e, t), R(e, "optin", (e) => e.def.innerType._zod.optin === "defaulted" ? "defaulted" : "optional"), e._zod.optout = "optional", R(e, "values", (e) => {
		let t = e.def.innerType._zod.values;
		return t ? /* @__PURE__ */ new Set([...t, void 0]) : void 0;
	}), R(e, "pattern", (e) => {
		let t = e.def.innerType._zod.pattern;
		return t ? RegExp(`^(${Rt(t.source)})?$`) : void 0;
	}), e._zod.parse = (e, n) => {
		if (e.value === void 0) {
			if (t.innerType._zod.optin !== "defaulted") return e;
			let r = t.innerType._zod.run({
				value: e.value,
				issues: []
			}, n);
			return r instanceof Promise ? r.then((t) => ha(e, t)) : ha(e, r);
		}
		return t.innerType._zod.run(e, n);
	};
}), _a = /*@__PURE__*/ z("$ZodExactOptional", (e, t) => {
	ga.init(e, t), R(e, "values", (e) => e.def.innerType._zod.values), R(e, "pattern", (e) => e.def.innerType._zod.pattern), e._zod.parse = (e, n) => t.innerType._zod.run(e, n);
}), va = /*@__PURE__*/ z("$ZodNullable", (e, t) => {
	W.init(e, t), R(e, "optin", (e) => e.def.innerType._zod.optin), R(e, "optout", (e) => e.def.innerType._zod.optout), R(e, "pattern", (e) => {
		let t = e.def.innerType._zod.pattern;
		return t ? RegExp(`^(${Rt(t.source)}|null)$`) : void 0;
	}), R(e, "values", (e) => e.def.innerType._zod.values ? /* @__PURE__ */ new Set([...e.def.innerType._zod.values, null]) : void 0), e._zod.parse = (e, n) => e.value === null ? e : t.innerType._zod.run(e, n);
}), ya = /*@__PURE__*/ z("$ZodDefault", (e, t) => {
	W.init(e, t), e._zod.optin = "defaulted", R(e, "values", (e) => e.def.innerType._zod.values), e._zod.parse = (e, n) => {
		if (n.direction === "backward") return t.innerType._zod.run(e, n);
		if (e.value === void 0) return e.value = t.defaultValue, e;
		let r = t.innerType._zod.run(e, n);
		return r instanceof Promise ? r.then((e) => ba(e, t)) : ba(r, t);
	};
});
function ba(e, t) {
	return e.value === void 0 && (e.value = t.defaultValue), e;
}
var xa = /*@__PURE__*/ z("$ZodPrefault", (e, t) => {
	W.init(e, t), e._zod.optin = "defaulted", R(e, "values", (e) => e.def.innerType._zod.values), e._zod.parse = (e, n) => (n.direction === "backward" || e.value === void 0 && (e.value = t.defaultValue), t.innerType._zod.run(e, n));
}), Sa = /*@__PURE__*/ z("$ZodNonOptional", (e, t) => {
	W.init(e, t), R(e, "values", (e) => {
		let t = e.def.innerType._zod.values;
		return t ? new Set([...t].filter((e) => e !== void 0)) : void 0;
	}), e._zod.parse = (n, r) => {
		let i = t.innerType._zod.run(n, r);
		return i instanceof Promise ? i.then((t) => Ca(t, e)) : Ca(i, e);
	};
});
function Ca(e, t) {
	return !e.issues.length && e.value === void 0 && e.issues.push({
		code: "invalid_type",
		expected: "nonoptional",
		input: e.value,
		inst: t
	}), e;
}
function wa(e, t, n, r) {
	return t.issues.length ? (e.value = n.catchValue({
		...t,
		value: e.value,
		error: { issues: t.issues.map((e) => I(e, r, H())) },
		input: e.value
	}), e) : (e.value = t.value, t.memo && (e.memo = !0), e);
}
var Ta = /*@__PURE__*/ z("$ZodCatch", (e, t) => {
	W.init(e, t), R(e, "optin", (e) => e.def.innerType._zod.optin === "defaulted" ? "defaulted" : "optional"), R(e, "optout", (e) => e.def.innerType._zod.optout), R(e, "values", (e) => e.def.innerType._zod.values), e._zod.parse = (e, n) => {
		if (n.direction === "backward") return t.innerType._zod.run(e, n);
		let r = t.innerType._zod.run({
			value: e.value,
			issues: []
		}, n);
		return r instanceof Promise ? r.then((r) => wa(e, r, t, n)) : wa(e, r, t, n);
	};
}), Ea = /*@__PURE__*/ z("$ZodPipe", (e, t) => {
	W.init(e, t), R(e, "values", (e) => e.def.in._zod.values), R(e, "optin", (e) => e.def.in._zod.optin), R(e, "optout", (e) => e.def.out._zod.optout), R(e, "propValues", (e) => e.def.in._zod.propValues), e._zod.parse = (e, n) => {
		if (n.direction === "backward") {
			let r = t.out._zod.run(e, n);
			return r instanceof Promise ? r.then((e) => Da(e, t.in, n)) : Da(r, t.in, n);
		}
		let r = t.in._zod.run(e, n);
		return r instanceof Promise ? r.then((e) => Da(e, t.out, n)) : Da(r, t.out, n);
	};
});
function Da(e, t, n) {
	return e.issues.some((e) => e.code !== "unrecognized_keys") ? (e.aborted = !0, e) : t._zod.run({
		value: e.value,
		issues: e.issues
	}, n);
}
var Oa = /*@__PURE__*/ z("$ZodReadonly", (e, t) => {
	W.init(e, t), R(e, "propValues", (e) => e.def.innerType._zod.propValues), R(e, "values", (e) => e.def.innerType._zod.values), R(e, "optin", (e) => e.def.innerType?._zod?.optin), R(e, "optout", (e) => e.def.innerType?._zod?.optout), e._zod.parse = (e, n) => {
		if (n.direction === "backward") return t.innerType._zod.run(e, n);
		let r = t.innerType._zod.run(e, n);
		return r instanceof Promise ? r.then(ka) : ka(r);
	};
});
function ka(e) {
	return e.memo || (e.value = Object.freeze(e.value)), e;
}
var Aa = /*@__PURE__*/ z("$ZodCustom", (e, t) => {
	U.init(e, t), W.init(e, t), e._zod.parse = (e, t) => e, e._zod.check = (n) => {
		let r = n.value, i = t.fn(r);
		if (i instanceof Promise) return i.then((t) => ja(t, n, r, e));
		ja(i, n, r, e);
	};
});
function ja(e, t, n, r) {
	if (!e) {
		let e = {
			code: "custom",
			input: n,
			inst: r,
			path: [...r._zod.def.path ?? []],
			continue: !r._zod.def.abort
		};
		r._zod.def.params && (e.params = r._zod.def.params), t.issues.push(xn(e));
	}
}
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/core/memoizer.js
var Ma = class extends Error {
	constructor() {
		super("Cannot parse a reference cycle that closes through a transform"), this.name = "ZodCyclicError";
	}
}, Na = "~memo", Pa = [];
function Fa(e) {
	return typeof e == "object" && !!e;
}
function Ia(e) {
	return e.map((e) => e.path ? {
		...e,
		path: e.path.slice()
	} : { ...e });
}
var La = /*@__PURE__*/ new WeakMap(), Ra = 0, za = 1, Ba = 2;
function Va(e, t, n) {
	let r = La.get(e);
	if (r !== void 0) return r ? Ba : Ra;
	if (t.has(e)) return Ba;
	t.add(e);
	let i = Ra, a = (e) => {
		if (i !== Ba && e?._zod) {
			let r = Va(e, t, n);
			r > i && (i = r);
		}
	}, o = (e, r) => {
		let i = Ra;
		for (let a of Reflect.ownKeys(e)) {
			let o = Object.getOwnPropertyDescriptor(e, a);
			if (r && !o.enumerable) continue;
			let s = o.get ? za : o.value?._zod ? Va(o.value, t, n) : Ra;
			s > i && (i = s);
		}
		return i;
	}, s = (e) => {
		e > i && (i = e);
	}, c = e._zod.def;
	switch (c.type) {
		case "object": {
			let e = zt(c);
			s(e ? o(e, !0) : za), a(c.catchall);
			break;
		}
		case "array":
			a(c.element);
			break;
		case "tuple":
			for (let e of c.items) a(e);
			a(c.rest);
			break;
		case "record":
		case "map":
			a(c.keyType), a(c.valueType);
			break;
		case "set":
			a(c.valueType);
			break;
		case "union":
			for (let e of c.options) a(e);
			break;
		case "intersection":
			a(c.left), a(c.right);
			break;
		case "optional":
		case "nullable":
		case "default":
		case "prefault":
		case "catch":
		case "readonly":
		case "nonoptional":
		case "promise":
		case "success":
			a(c.innerType);
			break;
		case "pipe":
			a(c.in), a(c.out);
			break;
		case "function":
			a(c.input), a(c.output);
			break;
		case "lazy": {
			let r = c._cachedInner ?? (n ? e._zod.innerType : void 0);
			s(r ? Va(r, t, !1) : za);
			break;
		}
		case "template_literal":
		case "string":
		case "number":
		case "int":
		case "boolean":
		case "bigint":
		case "symbol":
		case "undefined":
		case "null":
		case "void":
		case "never":
		case "any":
		case "unknown":
		case "date":
		case "nan":
		case "enum":
		case "literal":
		case "file":
		case "transform":
		case "custom": break;
		default: for (let e in c) {
			let t = Object.getOwnPropertyDescriptor(c, e);
			if (!t || t.get) continue;
			let n = t.value;
			if (n && typeof n == "object") {
				if (n._zod) a(n);
				else if (Array.isArray(n)) for (let e of n) a(e);
			}
		}
	}
	return t.delete(e), Ha(e, i);
}
function Ha(e, t) {
	return t !== za && La.set(e, t === Ba), t;
}
function Ua(e, t) {
	let n = e.buckets.get(t);
	return n || (n = /* @__PURE__ */ new WeakMap(), e.buckets.set(t, n)), n;
}
var Wa, Ga = [], Ka = {
	alloc(e, t, n) {
		let r = Wa;
		if (!r) return n;
		Wa = void 0;
		let i = {
			value: n,
			issues: null
		};
		return r.set(t.value, i), Ga.push(i), n;
	},
	guard(e) {
		var t;
		(t = e._zod).deferred ?? (t.deferred = []), e._zod.deferred.push(() => {
			let t = e._zod.parse, n = (e, n) => {
				if (n.direction !== "backward" && Ja(n, e.value)) throw new Ma();
				return t(e, n);
			};
			e._zod.parse = n, e._zod.run === t && (e._zod.run = n);
		});
	},
	attach(e) {
		var t;
		let n, r = !1, i, a;
		(t = e._zod).deferred ?? (t.deferred = []), e._zod.deferred.push(() => {
			let t = e._zod.parse, o = (s, c) => {
				if (n === void 0) {
					let i = Va(e, /* @__PURE__ */ new Set(), !1);
					if (i === Ra) return e._zod.parse = t, e._zod.run === o && (e._zod.run = t), t(s, c);
					i === Ba || r ? n = !0 : r = !0;
				}
				let l = s.value;
				if (!Fa(l)) return t(s, c);
				let u = c[Na];
				u || (u = {
					buckets: /* @__PURE__ */ new WeakMap(),
					backEdges: void 0
				}, c[Na] = u);
				let d;
				i === c ? d = a : (d = Ua(u, e), i = c, a = d);
				let f = d.get(l);
				if (f) return s.value = f.value, f.issues ? f.issues.length && s.issues.push(...Ia(f.issues)) : (s.memo = !0, u.backEdges ?? (u.backEdges = /* @__PURE__ */ new WeakSet()), u.backEdges.add(f.value)), s;
				Wa = d;
				let p = Ga.length, m = t(s, c);
				Wa = void 0;
				let h = Ga.length > p ? Ga.pop() : void 0;
				return m instanceof Promise ? m.then((e) => (h && (h.issues = e.issues.length ? Ia(e.issues) : Pa), e)) : (h && (h.issues = m.issues.length ? Ia(m.issues) : Pa), m);
			};
			e._zod.parse = o, e._zod.run === t && (e._zod.run = o);
		});
	}
};
function qa() {
	return Ka;
}
function Ja(e, t) {
	let n = e[Na]?.backEdges;
	return n !== void 0 && Fa(t) && n.has(t);
}
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/locales/en.js
var Ya = () => {
	let e = {
		string: {
			unit: "characters",
			verb: "to have"
		},
		file: {
			unit: "bytes",
			verb: "to have"
		},
		array: {
			unit: "items",
			verb: "to have"
		},
		set: {
			unit: "items",
			verb: "to have"
		},
		map: {
			unit: "entries",
			verb: "to have"
		}
	};
	function t(t) {
		return e[t] ?? null;
	}
	let n = {
		regex: "input",
		email: "email address",
		url: "URL",
		emoji: "emoji",
		uuid: "UUID",
		uuidv4: "UUIDv4",
		uuidv6: "UUIDv6",
		nanoid: "nanoid",
		guid: "GUID",
		cuid: "cuid",
		cuid2: "cuid2",
		ulid: "ULID",
		xid: "XID",
		ksuid: "KSUID",
		datetime: "ISO datetime",
		date: "ISO date",
		time: "ISO time",
		duration: "ISO duration",
		ipv4: "IPv4 address",
		ipv6: "IPv6 address",
		mac: "MAC address",
		cidrv4: "IPv4 range",
		cidrv6: "IPv6 range",
		base64: "base64-encoded string",
		base64url: "base64url-encoded string",
		json_string: "JSON string",
		e164: "E.164 number",
		currency_code: "currency code",
		credit_card: "credit card number",
		iban: "IBAN",
		jwt: "JWT",
		template_literal: "input"
	}, r = { nan: "NaN" };
	function i(e, t) {
		return e === "number" && typeof t == "number" && !Number.isFinite(t) ? String(t) : r[e] ?? e;
	}
	return (e) => {
		switch (e.code) {
			case "invalid_type": return `Invalid input: expected ${i(e.expected)}, received ${i(bn(e.input), e.input)}`;
			case "invalid_value": return e.values.length === 1 ? `Invalid input: expected ${Qt(e.values[0])}` : `Invalid option: expected one of ${Nt(e.values, "|")}`;
			case "too_big": {
				let n = e.exact ? "exactly " : e.inclusive ? "<=" : "<", r = t(e.origin);
				return r ? `Too big: expected ${e.origin ?? "value"} to have ${n}${e.maximum.toString()} ${r.unit ?? "elements"}` : `Too big: expected ${e.origin ?? "value"} to be ${n}${e.maximum.toString()}`;
			}
			case "too_small": {
				let n = e.exact ? "exactly " : e.inclusive ? ">=" : ">", r = t(e.origin);
				return r ? `Too small: expected ${e.origin} to have ${n}${e.minimum.toString()} ${r.unit}` : `Too small: expected ${e.origin} to be ${n}${e.minimum.toString()}`;
			}
			case "invalid_format": {
				let t = e;
				return t.format === "starts_with" ? `Invalid string: must start with "${t.prefix}"` : t.format === "ends_with" ? `Invalid string: must end with "${t.suffix}"` : t.format === "includes" ? `Invalid string: must include "${t.includes}"` : t.format === "regex" ? `Invalid string: must match pattern ${t.pattern}` : `Invalid ${n[t.format] ?? e.format}`;
			}
			case "not_multiple_of": return `Invalid number: must be a multiple of ${e.divisor}`;
			case "unrecognized_keys": return `Unrecognized key${e.keys.length > 1 ? "s" : ""}: ${Nt(e.keys, ", ")}`;
			case "invalid_key": return `Invalid key in ${e.origin}`;
			case "invalid_union": return e.options && Array.isArray(e.options) && e.options.length > 0 ? `Invalid discriminator value. Expected ${e.options.map((e) => `'${e}'`).join(" | ")}` : e.inclusive === !1 ? "Invalid input: more than one option matched" : "Invalid input";
			case "invalid_element": return `Invalid value in ${e.origin}`;
			default: return "Invalid input";
		}
	};
};
function Xa() {
	return { localeError: Ya() };
}
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/core/registries.js
var Za, Qa = class {
	constructor() {
		this._map = /* @__PURE__ */ new WeakMap(), this._idmap = /* @__PURE__ */ new Map();
	}
	add(e, ...t) {
		let n = t[0];
		return this._map.set(e, n), n && typeof n == "object" && "id" in n && this._idmap.set(n.id, e), this;
	}
	clear() {
		return this._map = /* @__PURE__ */ new WeakMap(), this._idmap = /* @__PURE__ */ new Map(), this;
	}
	remove(e) {
		let t = this._map.get(e);
		return t && typeof t == "object" && "id" in t && this._idmap.delete(t.id), this._map.delete(e), this;
	}
	get(e) {
		let t = e._zod.parent;
		if (t) {
			let n = { ...this.get(t) ?? {} };
			delete n.id;
			let r = {
				...n,
				...this._map.get(e)
			};
			return Object.keys(r).length ? r : void 0;
		}
		return this._map.get(e);
	}
	has(e) {
		return this._map.has(e);
	}
};
function $a() {
	return new Qa();
}
(Za = globalThis).__zod_globalRegistry ?? (Za.__zod_globalRegistry = $a());
var eo = globalThis.__zod_globalRegistry;
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/core/api.js
function to(e) {
	return e.checks &&= [...e.checks], e;
}
// @__NO_SIDE_EFFECTS__
function no(e, t) {
	return new e(to({
		type: "string",
		...P(t)
	}));
}
// @__NO_SIDE_EFFECTS__
function ro(e, t) {
	return new e({
		type: "string",
		format: "email",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function io(e, t) {
	return new e({
		type: "string",
		format: "guid",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function ao(e, t) {
	return new e({
		type: "string",
		format: "uuid",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function oo(e, t) {
	return new e({
		type: "string",
		format: "uuid",
		check: "string_format",
		abort: !1,
		version: "v4",
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function so(e, t) {
	return new e({
		type: "string",
		format: "uuid",
		check: "string_format",
		abort: !1,
		version: "v6",
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function co(e, t) {
	return new e({
		type: "string",
		format: "uuid",
		check: "string_format",
		abort: !1,
		version: "v7",
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function lo(e, t) {
	return new e({
		type: "string",
		format: "url",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function uo(e, t) {
	return new e({
		type: "string",
		format: "emoji",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function fo(e, t) {
	return new e({
		type: "string",
		format: "nanoid",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function po(e, t) {
	return new e({
		type: "string",
		format: "cuid",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function mo(e, t) {
	return new e({
		type: "string",
		format: "cuid2",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function ho(e, t) {
	return new e({
		type: "string",
		format: "ulid",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function go(e, t) {
	return new e({
		type: "string",
		format: "xid",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function _o(e, t) {
	return new e({
		type: "string",
		format: "ksuid",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function vo(e, t) {
	return new e({
		type: "string",
		format: "ipv4",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function yo(e, t) {
	return new e({
		type: "string",
		format: "ipv6",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function bo(e, t) {
	return new e({
		type: "string",
		format: "cidrv4",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function xo(e, t) {
	return new e({
		type: "string",
		format: "cidrv6",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function So(e, t) {
	return new e({
		type: "string",
		format: "base64",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Co(e, t) {
	return new e({
		type: "string",
		format: "base64url",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function wo(e, t) {
	return new e({
		type: "string",
		format: "e164",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function To(e, t) {
	return new e({
		type: "string",
		format: "jwt",
		check: "string_format",
		abort: !1,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Eo(e, t) {
	return new e({
		type: "string",
		format: "datetime",
		check: "string_format",
		offset: !1,
		local: !1,
		precision: null,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Do(e, t) {
	return new e({
		type: "string",
		format: "date",
		check: "string_format",
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Oo(e, t) {
	return new e({
		type: "string",
		format: "time",
		check: "string_format",
		precision: null,
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function ko(e, t) {
	return new e({
		type: "string",
		format: "duration",
		check: "string_format",
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Ao(e, t) {
	return new e({
		type: "boolean",
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function jo(e) {
	return new e({ type: "unknown" });
}
// @__NO_SIDE_EFFECTS__
function Mo(e, t) {
	return new e({
		type: "never",
		...P(t)
	});
}
// @__NO_SIDE_EFFECTS__
function No(e, t) {
	return new Kr({
		check: "max_length",
		...P(t),
		maximum: e
	});
}
// @__NO_SIDE_EFFECTS__
function Po(e, t) {
	return new qr({
		check: "min_length",
		...P(t),
		minimum: e
	});
}
// @__NO_SIDE_EFFECTS__
function Fo(e, t) {
	return new Jr({
		check: "length_equals",
		...P(t),
		length: e
	});
}
// @__NO_SIDE_EFFECTS__
function Io(e, t) {
	return new Xr({
		check: "string_format",
		format: "regex",
		...P(t),
		pattern: e
	});
}
// @__NO_SIDE_EFFECTS__
function Lo(e) {
	return new Zr({
		check: "string_format",
		format: "lowercase",
		...P(e)
	});
}
// @__NO_SIDE_EFFECTS__
function Ro(e) {
	return new Qr({
		check: "string_format",
		format: "uppercase",
		...P(e)
	});
}
// @__NO_SIDE_EFFECTS__
function zo(e, t) {
	return new $r({
		check: "string_format",
		format: "includes",
		...P(t),
		includes: e
	});
}
// @__NO_SIDE_EFFECTS__
function Bo(e, t) {
	return new ei({
		check: "string_format",
		format: "starts_with",
		...P(t),
		prefix: e
	});
}
// @__NO_SIDE_EFFECTS__
function Vo(e, t) {
	return new ti({
		check: "string_format",
		format: "ends_with",
		...P(t),
		suffix: e
	});
}
// @__NO_SIDE_EFFECTS__
function Ho(e) {
	return new ni({
		check: "overwrite",
		tx: e
	});
}
// @__NO_SIDE_EFFECTS__
function Uo(e) {
	return /* @__PURE__ */ Ho((t) => t.normalize(e));
}
// @__NO_SIDE_EFFECTS__
function Wo() {
	return /* @__PURE__ */ Ho((e) => e.trim());
}
// @__NO_SIDE_EFFECTS__
function Go() {
	return /* @__PURE__ */ Ho((e) => e.toLowerCase());
}
// @__NO_SIDE_EFFECTS__
function Ko() {
	return /* @__PURE__ */ Ho((e) => e.toUpperCase());
}
// @__NO_SIDE_EFFECTS__
function qo() {
	return /* @__PURE__ */ Ho((e) => Wt(e));
}
// @__NO_SIDE_EFFECTS__
function Jo(e, t, n) {
	return new e({
		type: "array",
		element: t,
		...P(n)
	});
}
// @__NO_SIDE_EFFECTS__
function Yo(e, t, n) {
	return new e({
		type: "custom",
		check: "custom",
		fn: t,
		...P(n)
	});
}
// @__NO_SIDE_EFFECTS__
function Xo(e, t) {
	let n = /* @__PURE__ */ Zo((t) => (t.addIssue = (e) => {
		if (typeof e == "string") t.issues.push(xn(e, t.value, n._zod.def));
		else {
			let r = e;
			r.fatal && (r.continue = !1), r.code ??= "custom", "input" in r || (r.input = t.value), r.inst ??= n, r.continue ??= !n._zod.def.abort, t.issues.push(xn(r));
		}
	}, e(t.value, t)), t);
	return n;
}
// @__NO_SIDE_EFFECTS__
function Zo(e, t) {
	let n = new U({
		check: "custom",
		...P(t)
	});
	return n._zod.check = e, n;
}
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/core/to-json-schema.js
function Qo(e, ...t) {
	for (let n of t) for (let t of Reflect.ownKeys(n)) Object.prototype.propertyIsEnumerable.call(n, t) && k(e, t, n[t]);
	return e;
}
function $o(e) {
	let t = e?.target ?? "draft-2020-12";
	return t === "draft-4" && (t = "draft-04"), t === "draft-7" && (t = "draft-07"), {
		processors: e.processors ?? {},
		metadataRegistry: e?.metadata ?? eo,
		target: t,
		unrepresentable: e?.unrepresentable ?? "throw",
		override: e?.override ?? (() => {}),
		io: e?.io ?? "output",
		counter: 0,
		seen: /* @__PURE__ */ new Map(),
		sharedDefsExtractedFor: void 0,
		sharedEmitDoneFor: void 0,
		cycles: e?.cycles ?? "ref",
		reused: e?.reused ?? "inline",
		intersections: [],
		deferred: [],
		external: e?.external ?? void 0
	};
}
function K(e, t, n, r, i) {
	let a = typeof t.unrepresentable == "function" ? t.unrepresentable({
		zodSchema: e,
		path: r.path,
		message: i
	}) : t.unrepresentable;
	if (a === "any") return !1;
	if (a === void 0 || a === "throw") throw Error(i);
	return Object.assign(n, a), !0;
}
function q(e, t, n = {
	path: [],
	schemaPath: []
}) {
	var r;
	let i = e._zod.def, a = t.seen.get(e);
	if (a) return a.count++, n.schemaPath.includes(e) && (a.cycle = n.path), a.schema;
	let o = {
		schema: {},
		count: 1,
		cycle: void 0,
		path: n.path
	};
	t.seen.set(e, o), t.sharedDefsExtractedFor = void 0, t.sharedEmitDoneFor = void 0;
	let s = e._zod.toJSONSchema?.();
	if (s) o.schema = s;
	else {
		let r = {
			...n,
			schemaPath: [...n.schemaPath, e],
			path: n.path
		};
		if (e._zod.processJSONSchema) e._zod.processJSONSchema(t, o.schema, r);
		else {
			let n = o.schema, a = t.processors[i.type];
			if (!a) throw Error(`[toJSONSchema]: Non-representable type encountered: ${i.type}`);
			a(e, t, n, r);
		}
		let a = e._zod.parent;
		a && (o.ref ||= a, q(a, t, r), t.seen.get(a).isParent = !0);
	}
	let c = t.metadataRegistry.get(e);
	return c && Qo(o.schema, c), t.io === "input" && J(e) && (delete o.schema.examples, delete o.schema.default), t.io === "input" && "_prefault" in o.schema && ((r = o.schema).default ?? (r.default = o.schema._prefault)), delete o.schema._prefault, t.seen.get(e).schema;
}
function es(e) {
	return e.replace(/~/g, "~0").replace(/\//g, "~1");
}
function ts(e, t) {
	let n = e.seen.get(t);
	if (!n) throw Error("Unprocessed schema. This is a bug in Zod.");
	if (e.external && e.sharedDefsExtractedFor === e.external) return;
	let r = /* @__PURE__ */ new Map();
	for (let t of e.seen.entries()) {
		let n = e.metadataRegistry.get(t[0])?.id;
		if (n) {
			let e = r.get(n);
			if (e && e !== t[0]) throw Error(`Duplicate schema id "${n}" detected during JSON Schema conversion. Two different schemas cannot share the same id when converted together.`);
			r.set(n, t[0]);
		}
	}
	let i = (t) => {
		let r = e.target === "draft-2020-12" ? "$defs" : "definitions";
		if (e.external) {
			let n = e.external.registry.get(t[0])?.id, i = e.external.uri ?? ((e) => e);
			if (n) return { ref: i(n) };
			let a = t[1].defId ?? t[1].schema.id ?? `schema${e.counter++}`;
			return t[1].defId = a, {
				defId: a,
				ref: `${i("__shared")}#/${r}/${es(a)}`
			};
		}
		let i = `#/${r}/`;
		if (t[1] === n && !t[1].schema.id) return { ref: "#" };
		let a = t[1].schema.id ?? `__schema${e.counter++}`;
		return {
			defId: a,
			ref: i + es(a)
		};
	}, a = (e) => {
		if (e[1].schema.$ref) return;
		let t = e[1], { ref: n, defId: r } = i(e);
		t.def = { ...t.schema }, r && (t.defId = r);
		let a = t.schema;
		for (let e in a) delete a[e];
		a.$ref = n;
	};
	if (e.cycles === "throw") for (let t of e.seen.entries()) {
		let e = t[1];
		if (e.cycle) throw Error(`Cycle detected: #/${e.cycle?.join("/")}/<root>

Set the \`cycles\` parameter to \`"ref"\` to resolve cyclical schemas with defs.`);
	}
	for (let n of e.seen.entries()) {
		let r = n[1];
		if (t === n[0]) a(n);
		else {
			if (e.external) {
				let r = e.external.registry.get(n[0])?.id;
				if (t !== n[0] && r) {
					a(n);
					continue;
				}
			}
			(e.metadataRegistry.get(n[0])?.id || r.cycle || r.count > 1 && e.reused === "ref") && a(n);
		}
	}
	e.external && (e.sharedDefsExtractedFor = e.external);
}
function ns(e) {
	let t = e.anyOf;
	if (!Array.isArray(t) || t.length === 0 || e.type !== void 0) return;
	let n = [];
	for (let e of t) {
		if (!e || typeof e != "object") return;
		ns(e);
		let t = Object.keys(e);
		if (t.length !== 1 || t[0] !== "type") return;
		let r = e.type;
		for (let e of Array.isArray(r) ? r : [r]) {
			if (typeof e != "string") return;
			n.includes(e) || n.push(e);
		}
	}
	delete e.anyOf, e.type = n.length === 1 ? n[0] : n;
}
var rs = /* @__PURE__ */ new Set([
	"type",
	"properties",
	"required",
	"additionalProperties"
]), is = ["oneOf", "anyOf"];
function as(e) {
	let t = e.additionalProperties;
	return t === void 0 || t === !1 || typeof t != "object" || !t ? null : Object.keys(t).length ? t : null;
}
function os(e) {
	let t = [];
	for (let n of e) {
		if (typeof n != "object" || n.type !== "object") return null;
		for (let e in n) if (!rs.has(e)) return null;
		t.push(n);
	}
	let n = {}, r = /* @__PURE__ */ new Set();
	for (let e of t) {
		for (let r in e.properties) {
			if (Object.prototype.hasOwnProperty.call(n, r)) continue;
			let e = [];
			for (let n of t) {
				let t = n.properties?.[r] ?? as(n);
				t != null && (e.some((e) => JSON.stringify(e) === JSON.stringify(t)) || e.push(t));
			}
			k(n, r, e.length === 1 ? e[0] : os(e) ?? { allOf: e });
		}
		for (let t of e.required ?? []) r.add(t);
	}
	let i = {
		type: "object",
		properties: n
	};
	if (r.size && (i.required = [...r]), t.every((e) => e.additionalProperties === !1)) i.additionalProperties = !1;
	else {
		let e = [];
		for (let n of t) {
			let t = as(n);
			t && !e.some((e) => JSON.stringify(e) === JSON.stringify(t)) && e.push(t);
		}
		e.length === 1 ? i.additionalProperties = e[0] : e.length > 1 && (i.additionalProperties = { allOf: e });
	}
	return i;
}
function ss(e) {
	let t = e.allOf;
	if (!Array.isArray(t) || t.length < 2) return;
	for (let t of rs) if (t in e) return;
	let n = t.filter((e) => is.some((t) => Array.isArray(e[t]))), r = null;
	if (!n.length) r = os(t);
	else {
		let e = n[0], i = is.find((t) => Array.isArray(e[t]));
		if (Object.keys(e).length !== 1) return;
		let a = t.filter((t) => t !== e), o = e[i].map((e) => os([...a, e]));
		if (o.some((e) => !e)) return;
		r = { [i]: o };
	}
	r && (delete e.allOf, Qo(e, r));
}
function cs(e, t) {
	let n = e.seen.get(t);
	if (!n) throw Error("Unprocessed schema. This is a bug in Zod.");
	let r = (t) => {
		let n = e.seen.get(t);
		if (n.ref === null) return;
		let i = n.def ?? n.schema, a = { ...i }, o = n.ref;
		if (n.ref = null, o) {
			r(o);
			let n = e.seen.get(o), s = n.schema;
			if (s.$ref && (e.target === "draft-07" || e.target === "draft-04" || e.target === "openapi-3.0") ? (i.allOf = i.allOf ?? [], i.allOf.push(s)) : Qo(i, s), Qo(i, a), t._zod.parent === o) for (let e in i) e !== "$ref" && e !== "allOf" && (e in a || delete i[e]);
			if (s.$ref && n.def) for (let e in i) e !== "$ref" && e !== "allOf" && e in n.def && JSON.stringify(i[e]) === JSON.stringify(n.def[e]) && delete i[e];
		}
		let s = t._zod.parent;
		if (s && s !== o) {
			r(s);
			let t = e.seen.get(s);
			if (t?.schema.$ref && (i.$ref = t.schema.$ref, t.def)) for (let e in i) e !== "$ref" && e !== "allOf" && e in t.def && JSON.stringify(i[e]) === JSON.stringify(t.def[e]) && delete i[e];
		}
		e.override({
			zodSchema: t,
			jsonSchema: i,
			path: n.path ?? []
		});
	};
	if (!e.external || e.sharedEmitDoneFor !== e.external) {
		for (let t of [...e.seen.entries()].reverse()) r(t[0]);
		if (e.target !== "openapi-3.0") for (let t of e.seen.entries()) ns(t[1].def ?? t[1].schema);
		for (let t of e.deferred) t();
		if (e.intersections.length) {
			let t = /* @__PURE__ */ new Map();
			for (let n of e.seen.values()) for (let e of [n.schema, n.def]) {
				let n = e?.allOf;
				if (!Array.isArray(n)) continue;
				let r = t.get(n);
				r ? r.push(e) : t.set(n, [e]);
			}
			for (let n of e.intersections) for (let e of t.get(n) ?? []) ss(e);
		}
	}
	let i = {};
	if (e.target === "draft-2020-12" ? i.$schema = "https://json-schema.org/draft/2020-12/schema" : e.target === "draft-07" ? i.$schema = "http://json-schema.org/draft-07/schema#" : e.target === "draft-04" ? i.$schema = "http://json-schema.org/draft-04/schema#" : e.target, e.external?.uri) {
		let n = e.external.registry.get(t)?.id;
		if (!n) throw Error("Schema is missing an `id` property");
		i.$id = e.external.uri(n);
	}
	Qo(i, n.defId ? n.schema : n.def ?? n.schema);
	let a = e.metadataRegistry.get(t)?.id;
	a !== void 0 && i.id === a && delete i.id;
	let o = e.external?.defs ?? {};
	if (!e.external || e.sharedEmitDoneFor !== e.external) for (let t of e.seen.entries()) {
		let e = t[1];
		e.def && e.defId && (e.def.id === e.defId && delete e.def.id, k(o, e.defId, e.def));
	}
	e.external && (e.sharedEmitDoneFor = e.external), e.external || Object.keys(o).length > 0 && (e.target === "draft-2020-12" ? i.$defs = o : i.definitions = o);
	try {
		let n = JSON.parse(JSON.stringify(i));
		return Object.defineProperty(n, "~standard", {
			value: {
				...t["~standard"],
				jsonSchema: {
					input: us(t, "input", e.processors),
					output: us(t, "output", e.processors)
				}
			},
			enumerable: !1,
			writable: !1
		}), n;
	} catch {
		throw Error("Error converting schema to JSON.");
	}
}
function J(e, t) {
	let n = t ?? { seen: /* @__PURE__ */ new Set() };
	if (n.seen.has(e)) return !1;
	n.seen.add(e);
	let r = e._zod.def;
	if (r.type === "transform") return !0;
	if (r.type === "array") return J(r.element, n);
	if (r.type === "set") return J(r.valueType, n);
	if (r.type === "lazy") return J(r.getter(), n);
	if (r.type === "promise" || r.type === "optional" || r.type === "nonoptional" || r.type === "nullable" || r.type === "readonly" || r.type === "default" || r.type === "prefault" || r.type === "catch") return J(r.innerType, n);
	if (r.type === "intersection") return J(r.left, n) || J(r.right, n);
	if (r.type === "record" || r.type === "map") return J(r.keyType, n) || J(r.valueType, n);
	if (r.type === "pipe") return e._zod.traits.has("$ZodCodec") ? !0 : J(r.in, n) || J(r.out, n);
	if (r.type === "object") {
		for (let e in r.shape) if (J(r.shape[e], n)) return !0;
		return !1;
	}
	if (r.type === "union") {
		for (let e of r.options) if (J(e, n)) return !0;
		return !1;
	}
	if (r.type === "tuple") {
		for (let e of r.items) if (J(e, n)) return !0;
		return !!(r.rest && J(r.rest, n));
	}
	return !1;
}
var ls = (e, t = {}) => (n) => {
	let r = $o({
		...n,
		processors: t
	});
	return q(e, r), ts(r, e), cs(r, e);
}, us = (e, t, n = {}) => (r) => {
	let { libraryOptions: i, target: a } = r ?? {}, o = $o({
		...i ?? {},
		target: a,
		io: t,
		processors: n
	});
	return q(e, o), ts(o, e), cs(o, e);
}, ds = (e, t, n) => {
	(e[t] === void 0 || n > e[t]) && (e[t] = n);
}, fs = (e, t, n) => {
	(e[t] === void 0 || n < e[t]) && (e[t] = n);
}, ps = (e, t) => {
	ds(e, "minimum", t), fs(e, "maximum", t);
}, ms = (e, t) => {
	e.multipleOf ??= [], e.multipleOf.includes(t) || e.multipleOf.push(t);
}, hs = (e, t) => {
	e.patterns ??= /* @__PURE__ */ new Set(), e.patterns.add(t);
}, gs = (e, t) => {
	e.mime = e.mime ? e.mime.filter((e) => t.includes(e)) : [...t];
}, _s = (e, t) => {
	e.format = t, t.includes("int") && (e.isInt = !0);
}, vs = (e, t) => ds(e, "minimum", t.minimum), ys = (e, t) => fs(e, "maximum", t.maximum), bs = (e) => (t, n) => {
	_s(t, n.format);
	let [r, i] = e[n.format];
	ds(t, "minimum", r), fs(t, "maximum", i);
}, xs = {
	greater_than: (e, t) => ds(e, t.inclusive ? "minimum" : "exclusiveMinimum", t.value),
	less_than: (e, t) => fs(e, t.inclusive ? "maximum" : "exclusiveMaximum", t.value),
	multiple_of: (e, t) => ms(e, t.value),
	number_format: bs(en),
	bigint_format: bs(tn),
	min_length: vs,
	max_length: ys,
	length_equals: (e, t) => ps(e, t.length),
	min_size: vs,
	max_size: ys,
	size_equals: (e, t) => ps(e, t.size),
	string_format: (e, t) => {
		_s(e, t.format), t.pattern && hs(e, t.pattern), (t.format === "base64" || t.format === "base64url") && (e.contentEncoding = t.format), (t.local || t.precision === -1) && (e.laxFormat = !0);
	},
	mime_type: (e, t) => gs(e, t.mime)
};
function Ss(e) {
	let t = {}, n = e._zod.def, r = e._zod.traits.has("$ZodCheck") ? [e, ...n.checks ?? []] : n.checks ?? [];
	for (let e of r) xs[e._zod.def.check]?.(t, e._zod.def);
	let i = e._zod.bag;
	i.minimum !== void 0 && ds(t, "minimum", i.minimum), i.exclusiveMinimum !== void 0 && ds(t, "exclusiveMinimum", i.exclusiveMinimum), i.maximum !== void 0 && fs(t, "maximum", i.maximum), i.exclusiveMaximum !== void 0 && fs(t, "exclusiveMaximum", i.exclusiveMaximum), i.multipleOf !== void 0 && ms(t, i.multipleOf), i.format !== void 0 && (t.format ??= i.format, i.format.includes("int") && (t.isInt = !0)), i.mime && gs(t, i.mime);
	for (let e of i.patterns ?? []) hs(t, e);
	return t;
}
var Cs = {
	guid: "uuid",
	url: "uri",
	datetime: "date-time",
	json_string: "json-string",
	regex: ""
}, ws = /* @__PURE__ */ new Map([[zi, kr], [Vi, Ar]]), Ts = (e) => ws.get(e) ?? e, Es = (e, t, n, r) => {
	let i = n;
	i.type = "string";
	let { minimum: a, maximum: o, format: s, patterns: c, contentEncoding: l, laxFormat: u } = Ss(e);
	if (typeof a == "number" && (i.minLength = a), typeof o == "number" && (i.maxLength = o), s && (i.format = Cs[s] ?? s, i.format === "" && delete i.format, (s === "time" || u) && delete i.format), l && (i.contentEncoding = l), c && c.size > 0) {
		let e = [...c].map(Ts);
		e.length === 1 ? i.pattern = e[0].source : e.length > 1 && (i.allOf = [...e.map((e) => ({
			...t.target === "draft-07" || t.target === "draft-04" || t.target === "openapi-3.0" ? { type: "string" } : {},
			pattern: e.source
		}))]);
	}
}, Ds = (e, t, n, r) => {
	n.type = "boolean";
}, Os = (e, t, n, r) => {
	n.not = {};
}, ks = (e, t, n, r) => {
	let i = e._zod.def, a = Mt(i.entries);
	a.length === 0 ? n.not = {} : (a.every((e) => typeof e == "number") && (n.type = "number"), a.every((e) => typeof e == "string") && (n.type = "string"), n.enum = a);
}, As = (e, t, n, r) => {
	let i = e._zod.def;
	if (i.values.length === 0) {
		n.not = {};
		return;
	}
	let a = [];
	for (let o of i.values) if (o === void 0) {
		if (K(e, t, n, r, "Literal `undefined` cannot be represented in JSON Schema")) return;
	} else if (typeof o == "bigint") {
		if (K(e, t, n, r, "BigInt literals cannot be represented in JSON Schema")) return;
		a.push(Number(o));
	} else a.push(o);
	if (a.length !== 0) {
		if (a.length === 1) {
			let e = a[0];
			n.type = e === null ? "null" : typeof e, t.target === "draft-04" || t.target === "openapi-3.0" ? n.enum = [e] : n.const = e;
		} else a.every((e) => typeof e == "number") && (n.type = "number"), a.every((e) => typeof e == "string") && (n.type = "string"), a.every((e) => typeof e == "boolean") && (n.type = "boolean"), a.every((e) => e === null) && (n.type = "null"), n.enum = a;
	}
}, js = (e, t, n, r) => {
	K(e, t, n, r, "Custom types cannot be represented in JSON Schema");
}, Ms = (e, t, n, r) => {
	K(e, t, n, r, "Transforms cannot be represented in JSON Schema");
}, Ns = (e, t, n, r) => {
	let i = n, a = e._zod.def, { minimum: o, maximum: s } = Ss(e);
	typeof o == "number" && (i.minItems = o), typeof s == "number" && (i.maxItems = s), i.type = "array", i.items = q(a.element, t, {
		...r,
		path: [...r.path, "items"]
	});
};
function Ps(e) {
	let t = e._zod.def;
	return t.type === "pipe" && t.in._zod.traits.has("$ZodTransform") ? Ps(t.out) : t.type === "catch" ? Ps(t.innerType) : e._zod.optin;
}
var Fs = (e, t, n, r) => {
	let i = n, a = e._zod.def, o = a.shape;
	if (Object.getOwnPropertySymbols(o).length && K(e, t, i, r, "Symbol keys cannot be represented in JSON Schema")) return;
	i.type = "object", i.properties = {};
	for (let e in o) k(i.properties, e, q(o[e], t, {
		...r,
		path: [
			...r.path,
			"properties",
			e
		]
	}));
	let s = [];
	for (let e of Object.keys(o)) {
		let n = a.shape[e];
		(t.io === "input" ? Ps(n) === void 0 : n._zod.optout === void 0) && s.push(e);
	}
	s.length > 0 && (i.required = s), a.catchall?._zod.def.type === "never" ? i.additionalProperties = !1 : a.catchall ? a.catchall && (i.additionalProperties = q(a.catchall, t, {
		...r,
		path: [...r.path, "additionalProperties"]
	})) : t.io === "output" && (i.additionalProperties = !1);
}, Is = (e, t, n, r) => {
	let i = e._zod.def, a = i.inclusive === !1, o = i.options.map((e, n) => q(e, t, {
		...r,
		path: [
			...r.path,
			a ? "oneOf" : "anyOf",
			n
		]
	}));
	a ? n.oneOf = o : n.anyOf = o;
}, Ls = (e, t, n, r) => {
	let i = e._zod.def, a = q(i.left, t, {
		...r,
		path: [
			...r.path,
			"allOf",
			0
		]
	}), o = q(i.right, t, {
		...r,
		path: [
			...r.path,
			"allOf",
			1
		]
	}), s = (e) => "allOf" in e && Object.keys(e).length === 1, c = [...s(a) ? a.allOf : [a], ...s(o) ? o.allOf : [o]];
	n.allOf = c, t.intersections.push(c);
};
function Rs(e, t, n) {
	if (t.$ref) {
		if (n.has(t)) return t;
		n.add(t);
		let r = e.get(t)?.def;
		if (!r) return t;
		let i = Rs(e, r, n);
		return i === r ? t : i;
	}
	for (let r of ["anyOf", "oneOf"]) {
		let i = t[r];
		if (!Array.isArray(i)) continue;
		let a = i.map((t) => Rs(e, t, n));
		a.some((e, t) => e !== i[t]) && (t = {
			...t,
			[r]: a
		});
	}
	let r = Array.isArray(t.type) ? t.type : [t.type], i = !r.includes("string") && r.some((e) => e === "number" || e === "integer"), a = t.enum ?? (t.const === void 0 ? void 0 : [t.const]);
	if (!i && !a?.some((e) => typeof e == "number")) return t;
	let { minimum: o, maximum: s, exclusiveMinimum: c, exclusiveMaximum: l, multipleOf: u, format: d, id: f, ...p } = t;
	return p.enum ? p.enum = p.enum.map((e) => typeof e == "number" ? String(e) : e) : typeof p.const == "number" && (p.const = String(p.const)), i && (p.type = "string", a || (p.pattern = (r.includes("number") ? Vr : Br).source)), p;
}
var zs = /* @__PURE__ */ new WeakMap();
function Bs(e) {
	let t = /* @__PURE__ */ new Map();
	for (let n of e.seen.values()) n.def && !t.has(n.schema) && t.set(n.schema, n);
	let n = /* @__PURE__ */ new Map();
	for (let r of zs.get(e) ?? []) {
		let i = e.seen.get(r), a = (i?.def ?? i?.schema)?.propertyNames;
		if (!a || a === !0 || n.has(a)) continue;
		let o = Rs(t, a, /* @__PURE__ */ new Set());
		o !== a && n.set(a, o);
	}
	if (n.size) for (let t of e.seen.values()) for (let e of [t.schema, t.def]) {
		let t = e && n.get(e.propertyNames);
		t && (e.propertyNames = t);
	}
}
var Vs = (e, t, n, r) => {
	let i = n, a = e._zod.def;
	i.type = "object";
	let o = a.keyType, s = Ss(o).patterns;
	if (a.mode === "loose" && s && s.size > 0) {
		let e = q(a.valueType, t, {
			...r,
			path: [
				...r.path,
				"patternProperties",
				"*"
			]
		});
		i.patternProperties = {};
		for (let t of s) k(i.patternProperties, Ts(t).source, e);
	} else {
		if (t.target === "draft-07" || t.target === "draft-2020-12") {
			i.propertyNames = q(a.keyType, t, {
				...r,
				path: [...r.path, "propertyNames"]
			});
			let n = zs.get(t);
			n || (n = [], zs.set(t, n), t.deferred.push(() => Bs(t))), n.push(e);
		}
		i.additionalProperties = q(a.valueType, t, {
			...r,
			path: [...r.path, "additionalProperties"]
		});
	}
	let c = o._zod.values, l = t.io === "input" && Ps(a.valueType) !== void 0;
	if (c && !a.partial && !l) {
		let e = [...c].filter((e) => typeof e == "string" || typeof e == "number");
		e.length > 0 && (i.required = e.map(String));
	}
}, Hs = (e, t, n, r) => {
	let i = e._zod.def, a = q(i.innerType, t, r), o = t.seen.get(e);
	t.target === "openapi-3.0" ? (o.ref = i.innerType, n.nullable = !0) : n.anyOf = [a, { type: "null" }];
}, Us = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType;
}, Ws = Symbol();
function Gs(e, t, n, r, i) {
	let a = !1, o = JSON.stringify(e, (e, t) => typeof t == "bigint" ? (a = !0, null) : t);
	return a ? (K(t, n, r, i, "BigInt defaults cannot be represented in JSON Schema"), Ws) : JSON.parse(o);
}
var Ks = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType;
	let o = Gs(i.defaultValue, e, t, n, r);
	o !== Ws && (n.default = o);
}, qs = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	if (a.ref = i.innerType, t.io !== "input") return;
	let o = Gs(i.defaultValue, e, t, n, r);
	o !== Ws && (n._prefault = o);
}, Js = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType;
	let o;
	try {
		o = i.catchValue(void 0);
	} catch {
		K(e, t, n, r, "Dynamic catch values are not supported in JSON Schema");
		return;
	}
	n.default = o;
}, Ys = (e, t, n, r) => {
	let i = e._zod.def, a = i.in._zod.traits.has("$ZodTransform"), o = t.io === "input" ? a ? i.out : i.in : i.out;
	q(o, t, r);
	let s = t.seen.get(e);
	s.ref = o;
}, Xs = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType, n.readOnly = !0;
}, Zs = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType;
}, Qs = /* @__PURE__ */ new WeakSet([Object.prototype, Error.prototype]);
function $s(e, t, n) {
	Object.defineProperty(e, t, {
		configurable: !0,
		enumerable: !1,
		get() {
			let e = n(this);
			return Object.defineProperty(this, t, {
				value: e,
				configurable: !0,
				writable: !0
			}), e;
		},
		set(e) {
			Object.defineProperty(this, t, {
				value: e,
				configurable: !0,
				writable: !0
			});
		}
	});
}
var Y = /*@__PURE__*/ z("ZodError", (e, t) => {
	Un.init(e, t), e.name = "ZodError";
	let n = Object.getPrototypeOf(e);
	Qs.has(n) || (Qs.add(n), $s(n, "format", (e) => (t) => Kn(e, t)), $s(n, "flatten", (e) => (t) => Gn(e, t)), $s(n, "addIssue", (e) => (t) => {
		e.issues.push(t), e.message = JSON.stringify(e.issues, Pt, 2);
	}), $s(n, "addIssues", (e) => (t) => {
		e.issues.push(...t), e.message = JSON.stringify(e.issues, Pt, 2);
	}), Object.defineProperty(n, "isEmpty", {
		configurable: !0,
		enumerable: !1,
		get() {
			return this.issues.length === 0;
		}
	}));
}, void 0, { Parent: Error }), ec = /* @__PURE__ */ Jn(Y), tc = /* @__PURE__ */ Yn(Y), nc = /* @__PURE__ */ Xn(Y), rc = /* @__PURE__ */ Qn(Y), ic = /* @__PURE__ */ ir(Y), ac = /* @__PURE__ */ ar(Y), oc = /* @__PURE__ */ or(Y), sc = /* @__PURE__ */ sr(Y), cc = /* @__PURE__ */ cr(Y), lc = /* @__PURE__ */ lr(Y), uc = /* @__PURE__ */ ur(Y), dc = /* @__PURE__ */ dr(Y);
// node_modules/.pnpm/zod@4.6.7/node_modules/zod/v4/classic/schemas.js
function fc() {
	V.localeError || H(Xa());
}
function pc() {
	V.memoizer || H({ memoizer: qa() });
}
var X = /*@__PURE__*/ z("ZodType", (e, t) => (fc(), W.init(e, t), e.def = t, e.type = t.type, e), {
	check(...e) {
		let t = this.def;
		return this.clone(M(t, { checks: [...t.checks ?? [], ...e.map((e) => typeof e == "function" ? { _zod: {
			check: e,
			def: { check: "custom" },
			onattach: []
		} } : e)] }), { parent: !0 });
	},
	with(...e) {
		return this.check(...e);
	},
	clone(e, t) {
		return N(this, e, t);
	},
	brand() {
		return this;
	},
	register(e, t) {
		return e.add(this, t), this;
	},
	refine(e, t) {
		return this.check(Tl(e, t));
	},
	superRefine(e, t) {
		return this.check(El(e, t));
	},
	overwrite(e) {
		return this.check(/* @__PURE__ */ Ho(e));
	},
	optional() {
		return sl(this);
	},
	exactOptional() {
		return ll(this);
	},
	nullable() {
		return dl(this);
	},
	nullish() {
		return sl(dl(this));
	},
	nonoptional(e) {
		return _l(this, e);
	},
	array() {
		return Kc(this);
	},
	or(e) {
		return Yc([this, e]);
	},
	and(e) {
		return Zc(this, e);
	},
	transform(e) {
		return xl(this, al(e));
	},
	default(e) {
		return pl(this, e);
	},
	prefault(e) {
		return hl(this, e);
	},
	catch(e) {
		return yl(this, e);
	},
	pipe(e) {
		return xl(this, e);
	},
	readonly() {
		return Cl(this);
	},
	describe(e) {
		let t = this.clone();
		return eo.add(t, { description: e }), t;
	},
	meta(...e) {
		if (e.length === 0) return eo.get(this);
		let t = this.clone();
		return eo.add(t, e[0]), t;
	},
	isOptional() {
		return this.safeParse(void 0).success;
	},
	isNullable() {
		return this.safeParse(null).success;
	},
	apply(e, ...t) {
		return t.length === 0 ? e(this) : e(this, ...t);
	},
	get "~standard"() {
		return Cn(this, "~standard", {
			...si(this),
			jsonSchema: {
				input: us(this, "input"),
				output: us(this, "output")
			}
		});
	},
	set "~standard"(e) {
		L(this, "~standard", e);
	},
	parse: function e(t, n) {
		return ec(this, t, n, { callee: e });
	},
	parseAsync: async function e(t, n) {
		return await tc(this, t, n, { callee: e });
	},
	safeParse(e, t) {
		return nc(this, e, t);
	},
	async safeParseAsync(e, t) {
		return rc(this, e, t);
	},
	get spa() {
		return this?.safeParseAsync;
	},
	set spa(e) {
		L(this, "spa", e);
	},
	validate(e, t) {
		return tr(this, e, t);
	},
	validateAsync(e, t) {
		return rr(this, e, t);
	},
	encode: function e(t, n) {
		return ic(this, t, n, { callee: e });
	},
	decode: function e(t, n) {
		return ac(this, t, n, { callee: e });
	},
	encodeAsync: async function e(t, n) {
		return await oc(this, t, n, { callee: e });
	},
	decodeAsync: async function e(t, n) {
		return await sc(this, t, n, { callee: e });
	},
	safeEncode(e, t) {
		return cc(this, e, t);
	},
	safeDecode(e, t) {
		return lc(this, e, t);
	},
	async safeEncodeAsync(e, t) {
		return uc(this, e, t);
	},
	async safeDecodeAsync(e, t) {
		return dc(this, e, t);
	},
	toJSONSchema(e) {
		return ls(this, {})(e);
	},
	get description() {
		return eo.get(this)?.description;
	},
	get _def() {
		return this._zod.def;
	}
}), mc = /*@__PURE__*/ z("_ZodString", (e, t) => {
	ci.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Es(e, t, n, r);
}, /*@__PURE__*/ wn({
	format: (e) => Ss(e).format ?? null,
	minLength: (e) => Ss(e).minimum ?? null,
	maxLength: (e) => Ss(e).maximum ?? null
}, {
	regex(...e) {
		return this.check(/* @__PURE__ */ Io(...e));
	},
	includes(...e) {
		return this.check(/* @__PURE__ */ zo(...e));
	},
	startsWith(...e) {
		return this.check(/* @__PURE__ */ Bo(...e));
	},
	endsWith(...e) {
		return this.check(/* @__PURE__ */ Vo(...e));
	},
	min(...e) {
		return this.check(/* @__PURE__ */ Po(...e));
	},
	max(...e) {
		return this.check(/* @__PURE__ */ No(...e));
	},
	length(...e) {
		return this.check(/* @__PURE__ */ Fo(...e));
	},
	nonempty(...e) {
		return this.check(/* @__PURE__ */ Po(1, ...e));
	},
	lowercase(e) {
		return this.check(/* @__PURE__ */ Lo(e));
	},
	uppercase(e) {
		return this.check(/* @__PURE__ */ Ro(e));
	},
	trim() {
		return this.check(/* @__PURE__ */ Wo());
	},
	normalize(...e) {
		return this.check(/* @__PURE__ */ Uo(...e));
	},
	toLowerCase() {
		return this.check(/* @__PURE__ */ Go());
	},
	toUpperCase() {
		return this.check(/* @__PURE__ */ Ko());
	},
	slugify() {
		return this.check(/* @__PURE__ */ qo());
	}
})), hc = /*@__PURE__*/ z("ZodString", (e, t) => {
	ci.init(e, t), mc.init(e, t);
}, {
	email(e) {
		return this.check(/* @__PURE__ */ ro(bc, e));
	},
	url(e) {
		return this.check(/* @__PURE__ */ lo(Cc, e));
	},
	jwt(e) {
		return this.check(/* @__PURE__ */ To(Rc, e));
	},
	emoji(e) {
		return this.check(/* @__PURE__ */ uo(wc, e));
	},
	guid(e) {
		return this.check(/* @__PURE__ */ io(xc, e));
	},
	uuid(e) {
		return this.check(/* @__PURE__ */ ao(Sc, e));
	},
	uuidv4(e) {
		return this.check(/* @__PURE__ */ oo(Sc, e));
	},
	uuidv6(e) {
		return this.check(/* @__PURE__ */ so(Sc, e));
	},
	uuidv7(e) {
		return this.check(/* @__PURE__ */ co(Sc, e));
	},
	nanoid(e) {
		return this.check(/* @__PURE__ */ fo(Tc, e));
	},
	cuid(e) {
		return this.check(/* @__PURE__ */ po(Ec, e));
	},
	cuid2(e) {
		return this.check(/* @__PURE__ */ mo(Dc, e));
	},
	ulid(e) {
		return this.check(/* @__PURE__ */ ho(Oc, e));
	},
	base64(e) {
		return this.check(/* @__PURE__ */ So(Fc, e));
	},
	base64url(e) {
		return this.check(/* @__PURE__ */ Co(Ic, e));
	},
	xid(e) {
		return this.check(/* @__PURE__ */ go(kc, e));
	},
	ksuid(e) {
		return this.check(/* @__PURE__ */ _o(Ac, e));
	},
	ipv4(e) {
		return this.check(/* @__PURE__ */ vo(jc, e));
	},
	ipv6(e) {
		return this.check(/* @__PURE__ */ yo(Mc, e));
	},
	cidrv4(e) {
		return this.check(/* @__PURE__ */ bo(Nc, e));
	},
	cidrv6(e) {
		return this.check(/* @__PURE__ */ xo(Pc, e));
	},
	e164(e) {
		return this.check(/* @__PURE__ */ wo(Lc, e));
	},
	datetime(e) {
		return this.check(/* @__PURE__ */ Eo(gc, e));
	},
	date(e) {
		return this.check(/* @__PURE__ */ Do(_c, e));
	},
	time(e) {
		return this.check(/* @__PURE__ */ Oo(vc, e));
	},
	duration(e) {
		return this.check(/* @__PURE__ */ ko(yc, e));
	}
});
function Z(e) {
	return /* @__PURE__ */ no(hc, e);
}
var Q = /*@__PURE__*/ z("ZodStringFormat", (e, t) => {
	G.init(e, t), mc.init(e, t);
}), gc = /*@__PURE__*/ z("ZodISODateTime", (e, t) => {
	Di.init(e, t), Q.init(e, t);
}), _c = /*@__PURE__*/ z("ZodISODate", (e, t) => {
	Oi.init(e, t), Q.init(e, t);
}), vc = /*@__PURE__*/ z("ZodISOTime", (e, t) => {
	ki.init(e, t), Q.init(e, t);
}), yc = /*@__PURE__*/ z("ZodISODuration", (e, t) => {
	Ai.init(e, t), Q.init(e, t);
}), bc = /*@__PURE__*/ z("ZodEmail", (e, t) => {
	di.init(e, t), Q.init(e, t);
}), xc = /*@__PURE__*/ z("ZodGUID", (e, t) => {
	li.init(e, t), Q.init(e, t);
}), Sc = /*@__PURE__*/ z("ZodUUID", (e, t) => {
	ui.init(e, t), Q.init(e, t);
}), Cc = /*@__PURE__*/ z("ZodURL", (e, t) => {
	yi.init(e, t), Q.init(e, t);
}), wc = /*@__PURE__*/ z("ZodEmoji", (e, t) => {
	bi.init(e, t), Q.init(e, t);
}), Tc = /*@__PURE__*/ z("ZodNanoID", (e, t) => {
	xi.init(e, t), Q.init(e, t);
}), Ec = /*@__PURE__*/ z("ZodCUID", (e, t) => {
	Si.init(e, t), Q.init(e, t);
}), Dc = /*@__PURE__*/ z("ZodCUID2", (e, t) => {
	Ci.init(e, t), Q.init(e, t);
}), Oc = /*@__PURE__*/ z("ZodULID", (e, t) => {
	wi.init(e, t), Q.init(e, t);
}), kc = /*@__PURE__*/ z("ZodXID", (e, t) => {
	Ti.init(e, t), Q.init(e, t);
}), Ac = /*@__PURE__*/ z("ZodKSUID", (e, t) => {
	Ei.init(e, t), Q.init(e, t);
}), jc = /*@__PURE__*/ z("ZodIPv4", (e, t) => {
	ji.init(e, t), Q.init(e, t);
}), Mc = /*@__PURE__*/ z("ZodIPv6", (e, t) => {
	Pi.init(e, t), Q.init(e, t);
}), Nc = /*@__PURE__*/ z("ZodCIDRv4", (e, t) => {
	Fi.init(e, t), Q.init(e, t);
}), Pc = /*@__PURE__*/ z("ZodCIDRv6", (e, t) => {
	Li.init(e, t), Q.init(e, t);
}), Fc = /*@__PURE__*/ z("ZodBase64", (e, t) => {
	Bi.init(e, t), Q.init(e, t);
}), Ic = /*@__PURE__*/ z("ZodBase64URL", (e, t) => {
	Ui.init(e, t), Q.init(e, t);
}), Lc = /*@__PURE__*/ z("ZodE164", (e, t) => {
	Wi.init(e, t), Q.init(e, t);
}), Rc = /*@__PURE__*/ z("ZodJWT", (e, t) => {
	Ki.init(e, t), Q.init(e, t);
}), zc = /*@__PURE__*/ z("ZodBoolean", (e, t) => {
	qi.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ds(e, t, n, r);
});
function Bc(e) {
	return /* @__PURE__ */ Ao(zc, e);
}
var Vc = /*@__PURE__*/ z("ZodUnknown", (e, t) => {
	Ji.init(e, t), X.init(e, t), e._zod.processJSONSchema = (e, t, n) => void 0;
});
function Hc() {
	return /* @__PURE__ */ jo(Vc);
}
var Uc = /*@__PURE__*/ z("ZodNever", (e, t) => {
	Yi.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Os(e, t, n, r);
});
function Wc(e) {
	return /* @__PURE__ */ Mo(Uc, e);
}
var Gc = /*@__PURE__*/ z("ZodArray", (e, t) => {
	pc(), $i.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ns(e, t, n, r), e.element = t.element;
}, {
	min(e, t) {
		return this.check(/* @__PURE__ */ Po(e, t));
	},
	nonempty(e) {
		return this.check(/* @__PURE__ */ Po(1, e));
	},
	max(e, t) {
		return this.check(/* @__PURE__ */ No(e, t));
	},
	length(e, t) {
		return this.check(/* @__PURE__ */ Fo(e, t));
	},
	unwrap() {
		return this.element;
	}
});
function Kc(e, t) {
	return /* @__PURE__ */ Jo(Gc, e, t);
}
var qc = /*@__PURE__*/ z("ZodObject", (e, t) => {
	pc(), aa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Fs(e, t, n, r), An(e, "shape", (e) => e._zod.def.shape, !1);
}, {
	keyof() {
		return tl(Object.keys(this._zod.def.shape));
	},
	catchall(e) {
		return this.clone(M(this._zod.def, { catchall: e }));
	},
	passthrough() {
		return this.clone(M(this._zod.def, { catchall: Hc() }));
	},
	loose() {
		return this.clone(M(this._zod.def, { catchall: Hc() }));
	},
	strict() {
		return this.clone(M(this._zod.def, { catchall: Wc() }));
	},
	strip() {
		return this.clone(M(this._zod.def, { catchall: void 0 }));
	},
	extend(e) {
		return on(this, e);
	},
	safeExtend(e) {
		return cn(this, e);
	},
	merge(e) {
		return ln(this, e);
	},
	pick(e) {
		return nn(this, e);
	},
	omit(e) {
		return an(this, e);
	},
	partial(...e) {
		return un(ol, this, e[0]);
	},
	exactPartial(...e) {
		return un(cl, this, e[0], "exactPartial");
	},
	required(...e) {
		return dn(gl, this, e[0]);
	}
});
function $(e, t) {
	return new qc({
		type: "object",
		shape: e ?? {},
		...P(t)
	});
}
var Jc = /*@__PURE__*/ z("ZodUnion", (e, t) => {
	sa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Is(e, t, n, r), e.options = t.options;
});
function Yc(e, t) {
	return new Jc({
		type: "union",
		options: e,
		...P(t)
	});
}
var Xc = /*@__PURE__*/ z("ZodIntersection", (e, t) => {
	ca.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ls(e, t, n, r);
});
function Zc(e, t) {
	return new Xc({
		type: "intersection",
		left: e,
		right: t
	});
}
var Qc = /*@__PURE__*/ z("ZodRecord", (e, t) => {
	pc(), da.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Vs(e, t, n, r), e.keyType = t.keyType, e.valueType = t.valueType;
});
function $c(e, t, n) {
	return new Qc({
		type: "record",
		keyType: e,
		valueType: t,
		...P(n),
		partial: !0
	});
}
var el = /*@__PURE__*/ z("ZodEnum", (e, t) => {
	fa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => ks(e, t, n, r), e.enum = t.entries, e.options = [...e._zod.values];
	let n = new Set(Object.keys(t.entries));
	e.extract = (e, r) => {
		let i = {};
		for (let r of e) if (n.has(r)) i[r] = t.entries[r];
		else throw Error(`Key ${r} not found in enum`);
		return new el({
			...t,
			checks: [],
			...P(r),
			entries: i
		});
	}, e.exclude = (e, r) => {
		let i = { ...t.entries };
		for (let t of e) if (n.has(t)) delete i[t];
		else throw Error(`Key ${t} not found in enum`);
		return new el({
			...t,
			checks: [],
			...P(r),
			entries: i
		});
	};
});
function tl(e, t) {
	return new el({
		type: "enum",
		entries: Array.isArray(e) ? Object.fromEntries(e.map((e) => [e, e])) : e,
		...P(t)
	});
}
var nl = /*@__PURE__*/ z("ZodLiteral", (e, t) => {
	pa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => As(e, t, n, r), e.values = new Set(t.values), Object.defineProperty(e, "value", { get() {
		if (t.values.length > 1) throw Error("This schema contains multiple valid literal values. Use `.values` instead.");
		return t.values[0];
	} });
});
function rl(e, t) {
	return new nl({
		type: "literal",
		values: Array.isArray(e) ? e : [e],
		...P(t)
	});
}
var il = /*@__PURE__*/ z("ZodTransform", (e, t) => {
	pc(), ma.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ms(e, t, n, r), e._zod.parse = (n, r) => {
		if (r.direction === "backward") throw new Ln(e.constructor.name);
		n.addIssue = (r) => {
			if (typeof r == "string") n.issues.push(xn(r, n.value, t));
			else {
				let t = r;
				t.fatal && (t.continue = !1), t.code ??= "custom", "input" in t || (t.input = n.value), t.inst ??= e, n.issues.push(xn(t));
			}
		};
		let i = t.transform(n.value, n);
		return i instanceof Promise ? i.then((e) => (n.value = e, n)) : (n.value = i, n);
	};
});
function al(e) {
	return new il({
		type: "transform",
		transform: e
	});
}
var ol = /*@__PURE__*/ z("ZodOptional", (e, t) => {
	ga.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Zs(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function sl(e) {
	return new ol({
		type: "optional",
		innerType: e
	});
}
var cl = /*@__PURE__*/ z("ZodExactOptional", (e, t) => {
	_a.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Zs(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function ll(e) {
	return new cl({
		type: "optional",
		innerType: e
	});
}
var ul = /*@__PURE__*/ z("ZodNullable", (e, t) => {
	va.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Hs(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function dl(e) {
	return new ul({
		type: "nullable",
		innerType: e
	});
}
var fl = /*@__PURE__*/ z("ZodDefault", (e, t) => {
	ya.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ks(e, t, n, r), e.unwrap = () => e._zod.def.innerType, e.removeDefault = e.unwrap;
});
function pl(e, t) {
	return new fl({
		type: "default",
		innerType: e,
		get defaultValue() {
			return typeof t == "function" ? t() : Yt(t);
		}
	});
}
var ml = /*@__PURE__*/ z("ZodPrefault", (e, t) => {
	xa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => qs(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function hl(e, t) {
	return new ml({
		type: "prefault",
		innerType: e,
		get defaultValue() {
			return typeof t == "function" ? t() : Yt(t);
		}
	});
}
var gl = /*@__PURE__*/ z("ZodNonOptional", (e, t) => {
	Sa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Us(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function _l(e, t) {
	return new gl({
		type: "nonoptional",
		innerType: e,
		...P(t)
	});
}
var vl = /*@__PURE__*/ z("ZodCatch", (e, t) => {
	Ta.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Js(e, t, n, r), e.unwrap = () => e._zod.def.innerType, e.removeCatch = e.unwrap;
});
function yl(e, t) {
	return new vl({
		type: "catch",
		innerType: e,
		catchValue: typeof t == "function" ? t : Mn(t)
	});
}
var bl = /*@__PURE__*/ z("ZodPipe", (e, t) => {
	Ea.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ys(e, t, n, r), e.in = t.in, e.out = t.out;
});
function xl(e, t) {
	return new bl({
		type: "pipe",
		in: e,
		out: t
	});
}
var Sl = /*@__PURE__*/ z("ZodReadonly", (e, t) => {
	Oa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Xs(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function Cl(e) {
	return new Sl({
		type: "readonly",
		innerType: e
	});
}
var wl = /*@__PURE__*/ z("ZodCustom", (e, t) => {
	Aa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => js(e, t, n, r);
});
function Tl(e, t = {}) {
	return /* @__PURE__ */ Yo(wl, e, t);
}
function El(e, t) {
	return /* @__PURE__ */ Xo(e, t);
}
// studio-src/gate-schemas.ts
var Dl = $({
	code_id: Z(),
	label: Z().nullable(),
	created_at: Z(),
	expires_at: Z().nullable()
}), Ol = $({
	enabled: Bc(),
	codes: Kc(Dl)
}), kl = $({ enabled: Bc() }), Al = $({
	code: Z(),
	code_id: Z(),
	expires_at: Z().nullable()
}), jl = $({ status: rl("revoked") });
// studio-src/gate-client.ts
function Ml(e, t) {
	return `${e}/gates/${encodeURIComponent(t)}`;
}
function Nl(e, t, n, r) {
	return e(Ml(t, n), Ol, { signal: r });
}
function Pl(e, t, n, r) {
	return e(Ml(t, n), kl, {
		method: "PUT",
		body: { enabled: r }
	});
}
function Fl(e, t, n, r) {
	return e(`${Ml(t, n)}/codes`, Al, {
		method: "POST",
		body: {
			label: r.label,
			expires_at: r.expires_at
		}
	});
}
function Il(e, t, n, r) {
	return e(`${Ml(t, n)}/codes/${encodeURIComponent(r)}`, jl, { method: "DELETE" });
}
// studio-src/keys.ts
function Ll(e) {
	return [
		"channel-web",
		"entry-gate",
		e
	];
}
function Rl(e) {
	return [
		"channel-web",
		"words",
		e
	];
}
// studio-src/MintEntryCodeDialog.tsx
var zl = {
	margin: 0,
	fontSize: "var(--tai-text-sm)",
	color: "var(--tai-color-text-muted)"
}, Bl = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--tai-space-4)"
};
function Vl({ base: t, identity: n, onClose: r }) {
	let o = ee(), l = ce(), [u, f] = C(""), [p, m] = C(""), [h, g] = C(null), [_, y] = C(null), x = Dt({
		mutationFn: (e) => Fl(o.request, t, n, e),
		onSuccess: (e) => {
			y(e), l.invalidateQueries({ queryKey: Ll(n) });
		}
	}), te = _ === null ? null : jt(o.baseUrl, t, n, _.code);
	return /* @__PURE__ */ w(a, {
		title: "Mint entry code",
		open: !0,
		dismissable: _ === null,
		onOpenChange: (e) => {
			e || r();
		},
		children: _ !== null && te !== null ? /* @__PURE__ */ T("div", {
			style: Bl,
			children: [
				/* @__PURE__ */ w(i, {
					value: _.code,
					label: "Entry code",
					caption: "This code is shown once — it cannot be shown again. Revoke and mint to get a new one."
				}),
				/* @__PURE__ */ w(i, {
					value: te,
					label: "Chat link",
					caption: "Anyone who opens this link enters the gated route."
				}),
				/* @__PURE__ */ w("p", {
					style: zl,
					children: _.expires_at === null ? "Never expires." : `Expires ${Ot(_.expires_at)}`
				}),
				/* @__PURE__ */ w("div", {
					style: {
						display: "flex",
						justifyContent: "flex-end"
					},
					children: /* @__PURE__ */ w(e, {
						type: "button",
						variant: "primary",
						onClick: r,
						children: "Done"
					})
				})
			]
		}) : /* @__PURE__ */ T("form", {
			"aria-label": "Mint entry code",
			onSubmit: (e) => {
				e.preventDefault(), g(null);
				let t = null;
				if (p !== "") {
					let e = new Date(p);
					if (e.getTime() <= Date.now()) {
						g("Expiry must be in the future.");
						return;
					}
					t = e.toISOString();
				}
				let n = u.trim();
				x.mutate({
					label: n === "" ? null : n,
					expires_at: t
				});
			},
			style: Bl,
			children: [
				/* @__PURE__ */ w(c, {
					label: "Label",
					description: "Optional. A human tag to tell codes apart.",
					children: /* @__PURE__ */ w(v, {
						value: u,
						placeholder: "e.g. newsletter",
						onChange: (e) => {
							f(e.target.value);
						}
					})
				}),
				/* @__PURE__ */ w(c, {
					label: "Expiry",
					description: "Optional. Leave blank for a code that never expires.",
					error: h ?? void 0,
					children: /* @__PURE__ */ w(v, {
						type: "datetime-local",
						value: p,
						onChange: (e) => {
							m(e.target.value);
						}
					})
				}),
				x.isError ? /* @__PURE__ */ w(s, { message: b(x.error) }) : null,
				/* @__PURE__ */ T("div", {
					style: {
						display: "flex",
						justifyContent: "flex-end",
						gap: "var(--tai-space-3)"
					},
					children: [/* @__PURE__ */ w(e, {
						type: "button",
						onClick: r,
						disabled: x.isPending,
						children: "Cancel"
					}), /* @__PURE__ */ T(e, {
						type: "submit",
						variant: "primary",
						disabled: x.isPending,
						children: [x.isPending ? /* @__PURE__ */ w(d, { label: "Minting" }) : null, "Mint code"]
					})]
				})
			]
		})
	});
}
// studio-src/query-client.ts
function Hl(e) {
	return e instanceof Error && e.name === "ApiUnauthorizedError";
}
function Ul(e) {
	let t = (t) => {
		Hl(t) && e();
	};
	return new pt({
		defaultOptions: {
			queries: { retry: !1 },
			mutations: { retry: !1 }
		},
		queryCache: new ft({ onError: t }),
		mutationCache: new lt({ onError: t })
	});
}
// studio-src/RevokeCodeDialog.tsx
function Wl({ isError: t, error: n, isPending: r, onCancel: i, onConfirm: o }) {
	return /* @__PURE__ */ w(a, {
		open: !0,
		title: "Revoke entry code",
		description: "Revoke this code? Its chat link stops working immediately and cannot be restored — a new code means revoke and mint.",
		onOpenChange: (e) => {
			e || i();
		},
		children: /* @__PURE__ */ T("div", {
			style: {
				display: "flex",
				flexDirection: "column",
				gap: "var(--tai-space-4)"
			},
			children: [t ? /* @__PURE__ */ w(s, { message: b(n) }) : null, /* @__PURE__ */ T("div", {
				style: {
					display: "flex",
					justifyContent: "flex-end",
					gap: "var(--tai-space-3)"
				},
				children: [/* @__PURE__ */ w(e, {
					onClick: i,
					disabled: r,
					children: "Cancel"
				}), /* @__PURE__ */ T(e, {
					variant: "danger",
					onClick: o,
					disabled: r,
					children: [r ? /* @__PURE__ */ w(d, { label: "Revoking" }) : null, "Revoke code"]
				})]
			})]
		})
	});
}
// studio-src/useEntryGate.ts
function Gl(e, t) {
	let n = ee(), r = ce(), [i, a] = C(!1), [o, s] = C(null), c = Et({
		queryKey: Ll(t),
		queryFn: ({ signal: r }) => Nl(n.request, e, t, r)
	}), l = () => {
		r.invalidateQueries({ queryKey: Ll(t) });
	}, u = Dt({
		mutationFn: (r) => Pl(n.request, e, t, r),
		onSuccess: () => {
			a(!1), l();
		}
	});
	return {
		gate: c,
		toggleMutation: u,
		revokeMutation: Dt({
			mutationFn: (r) => Il(n.request, e, t, r),
			onSuccess: () => {
				s(null), l();
			}
		}),
		onToggle: (e) => {
			u.reset(), e && c.data?.codes.length === 0 ? a(!0) : u.mutate(e);
		},
		confirmEnable: i,
		setConfirmEnable: a,
		pendingRevoke: o,
		setPendingRevoke: s
	};
}
// studio-src/EntryGatePanel.tsx
var Kl = {
	display: "flex",
	alignItems: "center",
	justifyContent: "space-between",
	gap: "var(--tai-space-3)",
	margin: "0 0 var(--tai-space-4)"
}, ql = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--tai-space-4)"
}, Jl = {
	margin: 0,
	fontSize: "var(--tai-text-lg)"
};
function Yl() {
	return /* @__PURE__ */ T(t, { children: [/* @__PURE__ */ w("div", {
		style: Kl,
		children: /* @__PURE__ */ w("h2", {
			style: Jl,
			children: "Entry gate"
		})
	}), /* @__PURE__ */ w(o, {
		title: "Entry gate unavailable",
		description: "The web channel's routes are not mounted on this deployment, so its entry gate cannot be read or changed here. Load the web channel to manage entry codes."
	})] });
}
function Xl({ route: e }) {
	let t = oe(), n = ne(), r = re(n);
	r.current = n;
	let [i] = C(() => Ul(() => {
		r.current();
	}));
	return t === null ? /* @__PURE__ */ w(Yl, {}) : /* @__PURE__ */ w(le, {
		client: i,
		children: /* @__PURE__ */ w(Zl, {
			base: t,
			identity: e.our_identity
		})
	});
}
function Zl({ base: i, identity: a }) {
	let [o, c] = C(!1), { gate: l, toggleMutation: d, revokeMutation: f, onToggle: p, confirmEnable: m, setConfirmEnable: h, pendingRevoke: g, setPendingRevoke: _ } = Gl(i, a), v;
	return v = l.isPending ? /* @__PURE__ */ w(u, { height: 120 }) : l.isError ? /* @__PURE__ */ w(s, {
		message: b(l.error),
		onRetry: () => void l.refetch()
	}) : /* @__PURE__ */ T("div", {
		style: ql,
		children: [
			/* @__PURE__ */ w(n, {
				label: "Require an entry code",
				checked: l.data.enabled,
				disabled: d.isPending,
				onCheckedChange: p
			}),
			d.isError && !m ? /* @__PURE__ */ w(s, { message: b(d.error) }) : null,
			/* @__PURE__ */ w(At, {
				codes: l.data.codes,
				onRevoke: (e) => {
					f.reset(), _(e);
				}
			})
		]
	}), /* @__PURE__ */ T(t, { children: [
		/* @__PURE__ */ T("div", {
			style: Kl,
			children: [/* @__PURE__ */ w("h2", {
				style: Jl,
				children: "Entry gate"
			}), /* @__PURE__ */ w(e, {
				variant: "primary",
				onClick: () => {
					c(!0);
				},
				children: "Mint entry code"
			})]
		}),
		v,
		o ? /* @__PURE__ */ w(Vl, {
			base: i,
			identity: a,
			onClose: () => {
				c(!1);
			}
		}) : null,
		m ? /* @__PURE__ */ w(r, {
			title: "Turn on the entry gate",
			confirmLabel: "Turn on anyway",
			pendingLabel: "Turning on",
			confirmVariant: "primary",
			isPending: d.isPending,
			error: d.isError ? d.error : null,
			onConfirm: () => {
				d.mutate(!0);
			},
			onClose: () => {
				d.reset(), h(!1);
			},
			children: /* @__PURE__ */ w("p", {
				style: { margin: 0 },
				children: "No live codes exist, so nobody can enter this route until you mint one. Turn the gate on anyway?"
			})
		}) : null,
		g === null ? null : /* @__PURE__ */ w(Wl, {
			isError: f.isError,
			error: f.error,
			isPending: f.isPending,
			onCancel: () => {
				_(null);
			},
			onConfirm: () => {
				f.mutate(g);
			}
		})
	] });
}
// studio-src/words-schemas.ts
var Ql = $({
	heading: Z().optional(),
	text: Z().optional()
}), $l = [
	{
		name: "not_a_navigation",
		label: "Not opened as a page",
		description: "The chat URL was loaded by something other than a top-level navigation (an embed or a script)."
	},
	{
		name: "link_invalid",
		label: "Link not valid",
		description: "The chat link's query parameters break a bound (a repeated name, a bad name, too long, too many)."
	},
	{
		name: "entry_refused",
		label: "Entry refused",
		description: "The route is gated and the link carries no live entry code."
	},
	{
		name: "page_unavailable",
		label: "Page unavailable",
		description: "The chat page bundle on the server cannot be served."
	}
], eu = [
	{
		name: "session_ended",
		label: "Session ended",
		description: "The visitor's session is gone (HTTP 401)."
	},
	{
		name: "wrong_site",
		label: "Wrong site",
		description: "The request came from another site (HTTP 403)."
	},
	{
		name: "not_listening",
		label: "Not listening",
		description: "No route answers this chat (HTTP 404)."
	},
	{
		name: "already_answered",
		label: "Already answered",
		description: "The question was already answered (409)."
	},
	{
		name: "busy",
		label: "Busy",
		description: "The assistant is shedding load (HTTP 429 or 503)."
	},
	{
		name: "not_switched_on",
		label: "Not switched on",
		description: "The deployment runs no chat store (501)."
	},
	{
		name: "too_long",
		label: "Too long",
		description: "The request is over the size cap (HTTP 413). Token: {subject}."
	},
	{
		name: "not_sent",
		label: "Not sent",
		description: "The door refused the request's content (HTTP 400 or 422). Token: {subject}."
	},
	{
		name: "failed",
		label: "Failed",
		description: "Any other failure of the door."
	},
	{
		name: "attachment_gone",
		label: "Attachment gone",
		description: "An attachment can no longer be sent with the message."
	},
	{
		name: "attachment_already_sent",
		label: "Attachment already sent",
		description: "An attachment was already sent with another message."
	},
	{
		name: "entry_refused",
		label: "Entry refused",
		description: "A new conversation was refused by the route's entry gate."
	},
	{
		name: "answer_refused",
		label: "Answer refused",
		description: "An answer was refused and may be given again, when the send set no sentence. Token: {reason}."
	},
	{
		name: "answer_retries_spent",
		label: "Answer retries spent",
		description: "An answer was refused and the question can no longer be answered, when the send set no sentence. Token: {reason}."
	},
	{
		name: "subject_message",
		label: "Subject: message",
		description: "The {subject} of a chat message."
	},
	{
		name: "subject_answer",
		label: "Subject: answer",
		description: "The {subject} of a question's answer."
	},
	{
		name: "subject_form",
		label: "Subject: form",
		description: "The {subject} of a form."
	},
	{
		name: "subject_request",
		label: "Subject: request",
		description: "The {subject} of any other request (a new conversation, the chat stream)."
	}
], tu = $({
	page: $c(tl($l.map((e) => e.name)), Ql).optional(),
	transport: $c(tl(eu.map((e) => e.name)), Z()).optional()
}), nu = $({ status: rl("cleared") });
// studio-src/words-client.ts
function ru(e, t) {
	return `${e}/words/${encodeURIComponent(t)}`;
}
function iu(e) {
	return e instanceof Error && e.name === "ApiError" && e.status === 404;
}
async function au(e, t, n, r) {
	try {
		return await e(ru(t, n), tu, { signal: r });
	} catch (e) {
		if (iu(e)) return null;
		throw e;
	}
}
function ou(e, t, n, r) {
	return e(ru(t, n), tu, {
		method: "PUT",
		body: r
	});
}
function su(e, t, n) {
	return e(ru(t, n), nu, { method: "DELETE" });
}
// studio-src/WordsPanel.tsx
var cu = {
	display: "flex",
	alignItems: "center",
	justifyContent: "space-between",
	gap: "var(--tai-space-3)",
	margin: "0 0 var(--tai-space-4)"
}, lu = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--tai-space-4)"
}, uu = {
	display: "flex",
	alignItems: "center",
	gap: "var(--tai-space-3)"
}, du = {
	margin: 0,
	fontSize: "var(--tai-text-lg)"
}, fu = {
	margin: 0,
	fontSize: "var(--tai-text-md)"
}, pu = {
	margin: 0,
	color: "var(--tai-color-text-muted)"
}, mu = "No words are set for this route, so every page and sentence draws the built-in English.";
function hu(e) {
	return `page.${e}.heading`;
}
function gu(e) {
	return `page.${e}.text`;
}
function _u(e) {
	return `transport.${e}`;
}
var vu = /* @__PURE__ */ new Set([...$l.flatMap(({ name: e }) => [hu(e), gu(e)]), ...eu.map(({ name: e }) => _u(e))]);
function yu(e) {
	let t = {};
	for (let { name: n } of $l) {
		let r = e?.page?.[n];
		t[hu(n)] = r?.heading ?? "", t[gu(n)] = r?.text ?? "";
	}
	for (let { name: n } of eu) t[_u(n)] = e?.transport?.[n] ?? "";
	return t;
}
function bu(e) {
	let t = {};
	for (let { name: n } of $l) {
		let r = e[hu(n)] ?? "", i = e[gu(n)] ?? "";
		(r !== "" || i !== "") && (t[n] = {
			...r === "" ? {} : { heading: r },
			...i === "" ? {} : { text: i }
		});
	}
	let n = {};
	for (let { name: t } of eu) {
		let r = e[_u(t)] ?? "";
		r !== "" && (n[t] = r);
	}
	return {
		...Object.keys(t).length === 0 ? {} : { page: t },
		...Object.keys(n).length === 0 ? {} : { transport: n }
	};
}
function xu(e) {
	if (!(e instanceof Error) || e.name !== "ApiError" || e.status !== 422) return null;
	let t = /^invalid request body: ([^:]+): (.+)$/s.exec(e.message);
	if (t === null) return null;
	let [, n, r] = t;
	return n === void 0 || r === void 0 || !vu.has(n) ? null : {
		path: n,
		reason: r
	};
}
function Su() {
	return /* @__PURE__ */ T(t, { children: [/* @__PURE__ */ w("div", {
		style: cu,
		children: /* @__PURE__ */ w("h2", {
			style: du,
			children: "Words"
		})
	}), /* @__PURE__ */ w(o, {
		title: "Words unavailable",
		description: "The web channel's routes are not mounted on this deployment, so a route's words cannot be read or changed here. Load the web channel to manage them."
	})] });
}
function Cu({ route: e }) {
	let t = oe(), n = ne(), r = re(n);
	r.current = n;
	let [i] = C(() => Ul(() => {
		r.current();
	}));
	return t === null ? /* @__PURE__ */ w(Su, {}) : /* @__PURE__ */ w(le, {
		client: i,
		children: /* @__PURE__ */ w(wu, {
			base: t,
			identity: e.our_identity
		})
	});
}
function wu({ base: e, identity: n }) {
	let r = ee(), i = Et({
		queryKey: Rl(n),
		queryFn: ({ signal: t }) => au(r.request, e, n, t)
	}), a;
	return a = i.isPending ? /* @__PURE__ */ w(u, { height: 240 }) : i.isError ? /* @__PURE__ */ w(s, {
		message: b(i.error),
		onRetry: () => void i.refetch()
	}) : /* @__PURE__ */ w(Tu, {
		base: e,
		identity: n,
		stored: i.data
	}), /* @__PURE__ */ T(t, { children: [/* @__PURE__ */ w("div", {
		style: cu,
		children: /* @__PURE__ */ w("h2", {
			style: du,
			children: "Words"
		})
	}), a] });
}
function Tu({ base: t, identity: n, stored: i }) {
	let a = ee(), o = ce(), [l, u] = C(() => yu(i)), [d, f] = C({}), [p, m] = C(!1), [h, g] = C(!1), _ = Dt({
		mutationFn: (e) => ou(a.request, t, n, e),
		onSuccess: (e) => {
			o.setQueryData(Rl(n), e), u(yu(e)), m(!0);
		},
		onError: (e) => {
			let t = xu(e);
			t !== null && f({ [t.path]: t.reason });
		}
	}), y = Dt({
		mutationFn: () => su(a.request, t, n),
		onSuccess: () => {
			o.setQueryData(Rl(n), null), u(yu(null)), f({}), m(!1), g(!1);
		}
	}), x = (e, t) => {
		u((n) => ({
			...n,
			[e]: t
		})), m(!1), f((t) => {
			if (!(e in t)) return t;
			let { [e]: n, ...r } = t;
			return r;
		});
	}, te = (e, t, n) => /* @__PURE__ */ w(c, {
		label: t,
		...n === void 0 ? {} : { description: n },
		...d[e] === void 0 ? {} : { error: d[e] },
		children: /* @__PURE__ */ w(v, {
			value: l[e] ?? "",
			onChange: (t) => {
				x(e, t.target.value);
			}
		})
	}, e), ne = bu(l), S = ne.page === void 0 && ne.transport === void 0;
	return /* @__PURE__ */ T("div", {
		style: lu,
		children: [
			i === null ? /* @__PURE__ */ w("p", {
				style: pu,
				children: mu
			}) : null,
			/* @__PURE__ */ w("h3", {
				style: fu,
				children: "Refusal pages"
			}),
			/* @__PURE__ */ w("p", {
				style: pu,
				children: "The pages the chat link serves in place of the chat when it refuses a visit."
			}),
			$l.map(({ name: e, label: t, description: n }) => [te(hu(e), `${t}: heading`, n), te(gu(e), `${t}: text`)]),
			/* @__PURE__ */ w("h3", {
				style: fu,
				children: "Chat sentences"
			}),
			/* @__PURE__ */ w("p", {
				style: pu,
				children: "The sentences the chat draws when one of its requests is refused."
			}),
			eu.map(({ name: e, label: t, description: n }) => te(_u(e), t, n)),
			_.isError && xu(_.error) === null ? /* @__PURE__ */ w(s, { message: b(_.error) }) : null,
			/* @__PURE__ */ T("div", {
				style: uu,
				children: [
					/* @__PURE__ */ w(e, {
						variant: "primary",
						disabled: _.isPending || S,
						onClick: () => {
							f({}), m(!1), _.mutate(ne);
						},
						children: "Save words"
					}),
					i === null ? null : /* @__PURE__ */ w(e, {
						variant: "danger",
						onClick: () => {
							y.reset(), g(!0);
						},
						children: "Clear words"
					}),
					p ? /* @__PURE__ */ w("span", {
						role: "status",
						children: "Words saved."
					}) : null
				]
			}),
			h ? /* @__PURE__ */ w(r, {
				title: "Clear the words",
				confirmLabel: "Clear words",
				pendingLabel: "Clearing",
				confirmVariant: "danger",
				isPending: y.isPending,
				error: y.isError ? y.error : null,
				onConfirm: () => {
					y.mutate();
				},
				onClose: () => {
					y.reset(), g(!1);
				},
				children: /* @__PURE__ */ w("p", {
					style: { margin: 0 },
					children: "Every page and sentence of this route will draw the built-in English again."
				})
			}) : null
		]
	});
}
// studio-src/WebRoutePanel.tsx
function Eu(e) {
	return `${e}/gates`;
}
function Du(e) {
	return `${e}/words`;
}
var Ou = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--tai-space-4)"
};
function ku(e) {
	let t = oe(), { state: n } = te();
	if (n.status !== "ready") throw Error("the web route panel rendered without a ready capability projection");
	let { projection: r } = n, i = (e) => x(r) || y(r, [e]), a = t === null || i(Eu(t)), o = t === null || i(Du(t));
	return /* @__PURE__ */ T("div", {
		style: Ou,
		children: [a ? /* @__PURE__ */ w(Xl, { ...e }) : null, o ? /* @__PURE__ */ w(Cu, { ...e }) : null]
	});
}
// studio-src/index.tsx
function Au(e) {
	let t = e.routeBase("web");
	ae(t), e.registerRoutePanel({
		channel: "web",
		title: "Web chat",
		component: ku,
		...t === null ? {} : { requiredCapabilities: { routes: [Eu(t), Du(t)] } }
	});
}
export { Au as register };
