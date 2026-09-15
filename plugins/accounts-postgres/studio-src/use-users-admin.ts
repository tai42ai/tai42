/**
 * The users-admin data source: the list of accounts and the role templates, loaded
 * together from this plugin's own `/api/auth/*` routes and reloadable on demand.
 * A load is aborted if the component unmounts or a reload supersedes it, and a
 * failure surfaces as a message rather than a blank list.
 */
import { errorMessage } from '@tai42/studio-sdk';
import { useCallback, useEffect, useState } from 'react';

import type { AdminUser, RoleTemplate, UsersAdminApi } from '@/api';
import { useUsersAdminApi } from '@/api';

export interface UsersAdmin {
  readonly api: UsersAdminApi;
  readonly users: AdminUser[] | null;
  readonly roles: RoleTemplate[];
  readonly loadError: string | null;
  readonly loading: boolean;
  readonly reload: () => void;
}

export function useUsersAdmin(): UsersAdmin {
  const api = useUsersAdminApi();

  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [roles, setRoles] = useState<RoleTemplate[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);

  const reload = useCallback(() => {
    setVersion((v) => v + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    Promise.all([api.listUsers(controller.signal), api.listRoles(controller.signal)]).then(
      ([nextUsers, nextRoles]) => {
        if (controller.signal.aborted) return;
        setUsers(nextUsers);
        setRoles(nextRoles);
        setLoading(false);
      },
      (err: unknown) => {
        if (controller.signal.aborted) return;
        setLoadError(errorMessage(err));
        setLoading(false);
      },
    );
    return () => {
      controller.abort();
    };
  }, [api, version]);

  return { api, users, roles, loadError, loading, reload };
}
