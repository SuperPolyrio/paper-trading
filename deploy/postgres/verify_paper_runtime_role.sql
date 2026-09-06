\set ON_ERROR_STOP on

SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'poly_quant_paper_app'
      AND rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolreplication
      AND NOT rolbypassrls
) THEN 1 ELSE 0 END AS paper_login_is_least_privilege;

SELECT CASE WHEN pg_has_role(
    'poly_quant_paper_app', 'poly_quant_paper_runtime', 'MEMBER'
) THEN 1 ELSE 0 END AS paper_capability_membership;

SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM pg_auth_members membership
    JOIN pg_roles member_role ON member_role.oid = membership.member
    JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
    WHERE member_role.rolname = 'poly_quant_paper_app'
      AND granted_role.rolname ~* '(live|calibration|order|trader)'
) THEN 1 ELSE 0 END AS no_live_role_membership;
