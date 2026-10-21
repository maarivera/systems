# User Offboarding with Event-Driven Ansible

When the HR system reports that someone has left, an Event-Driven Ansible (EDA) rulebook starts a playbook that removes that person's access from:

| System | What happens |
|---|---|
| **Okta (SSO)** | Clears sessions and OAuth tokens, then deactivates (or suspends) the user. |
| **Entra ID (Azure AD)** | Blocks sign-in, revokes sessions, removes groups and directory roles, and optionally removes licences. |
| **Email (Exchange Online)** | Turns on an auto-reply that points people to the manager. Can also forward new mail. |
| **Microsoft Teams** | If the user is a team's only owner, makes their manager an owner. Then removes the user from every team. |
| **Azure** | Removes the user's role assignments in the subscriptions you list. |
| **AWS** | For IAM users, deactivates access keys, removes the console password and groups, and tags the user. For IAM Identity Center, removes group memberships and direct account assignments. |

The work runs in three stages:

1. **Cut access** in Okta, Entra ID and AWS.
2. **Hand over work** in email and Teams.
3. **Remove entitlements**: Azure RBAC, Entra groups, roles and licences.

If one system fails, the error is recorded and the other systems still run. The run fails at the end so the problem gets noticed.

Accounts are **disabled, not deleted**, so you keep the audit trail and can still recover data. Deletion should stay a separate, later step under your retention policy.

## Safety

- **Dry run by default.** `offboard_dry_run: true` looks the user up everywhere and reports what *would* change, without changing anything. Set it to `false` only after a dry run against a test user looks right.
- **Protected accounts.** Accounts listed in `protected_accounts` (such as break-glass admins and service accounts) are never offboarded.
- **Typo guard.** If the UPN is not found in any enabled system, the run fails with `NOT FOUND`.
- **No orphaned teams.** A team that would be left with no owner is not touched, and a warning is reported.
- **Evidence.** Each run writes `reports/offboarding_<user>_<timestamp>.log`, listing every planned or completed action plus any errors, ready to attach to the HR or change ticket.

## Requirements

- **Control node:** `ansible-core` 2.15 or later. To run the rulebook, also install `ansible-rulebook` (needs Java 17+) and `ansible-galaxy collection install -r requirements.yml`.
- **AWS CLI v2**, with one named profile per account (only needed if AWS is enabled).
- The playbook uses only `ansible.builtin` modules. Microsoft and Okta are called through their REST APIs, so no cloud collections are needed.

### Permissions

**Entra app registration** (client credentials). Grant these *application* permissions and give admin consent:

| Permission | Used for |
|---|---|
| `User.ReadWrite.All` | Look up the user and manager, block sign-in, revoke sessions, remove licences |
| `GroupMember.ReadWrite.All` | Remove the user from groups |
| `RoleManagement.ReadWrite.Directory` | Remove the user from directory roles |
| `MailboxSettings.ReadWrite` | Set the auto-reply and the forwarding rule |
| `Team.ReadBasic.All`, `TeamMember.ReadWrite.All` | Teams ownership hand-over and removal |

To disable users who hold admin roles, the app also needs a matching Entra role, such as *Privileged Authentication Administrator*. For Azure RBAC, give the app's service principal **User Access Administrator** on each subscription in `azure_subscriptions`.

**Okta:** an API token from an admin who can manage users (Org Admin or a custom role with user lifecycle rights).

**AWS IAM:** `iam:GetUser`, `ListAccessKeys`, `UpdateAccessKey`, `GetLoginProfile`, `DeleteLoginProfile`, `ListGroupsForUser`, `RemoveUserFromGroup`, `ListAttachedUserPolicies` and `TagUser`. For Identity Center, also `identitystore:GetUserId`, `ListGroupMembershipsForMember` and `DeleteGroupMembership`, plus `sso:ListAccountAssignmentsForPrincipal` and `DeleteAccountAssignment`.

### Secrets

Secrets are read from environment variables and are never stored in the repo:

```bash
export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
export OKTA_ORG_URL=https://example.okta.com OKTA_API_TOKEN=...
export WEBHOOK_TOKEN=$(openssl rand -hex 32)      # shared with the HR system
```

On Ansible Automation Platform, use credentials that inject these variables.

## Quick start

1. Review `inventory/group_vars/all.yml`. Set `protected_accounts`, `aws_accounts`, `azure_subscriptions`, the Identity Center IDs if you use it, and switch off any system you don't have in `offboard_systems`.
2. Do a dry run by hand against a test user:

   ```bash
   cd offboarding
   ansible-playbook site.yml -e offboard_upn=test.user@example.com
   cat reports/offboarding_test.user_example.com_*.log
   ```

3. Run it for real by hand:

   ```bash
   ansible-playbook site.yml -e offboard_upn=test.user@example.com -e offboard_dry_run=false
   ```

4. Start the rulebook:

   ```bash
   ansible-rulebook -r rulebooks/offboarding.yml -i inventory/hosts.ini --env-vars WEBHOOK_TOKEN --verbose
   ```

5. Send a test event, the same way the HR system will:

   ```bash
   curl -X POST http://localhost:5000/endpoint \
     -H "Authorization: Bearer $WEBHOOK_TOKEN" \
     -H "Content-Type: application/json" \
     -d @examples/termination_event.json
   ```

The rulebook does **not** override `offboard_dry_run`. EDA-triggered runs stay dry runs until you set `offboard_dry_run: false` in group vars.

## Usage examples

```bash
ansible-playbook site.yml -e offboard_upn=jane.doe@example.com                      # dry run
ansible-playbook site.yml -e offboard_upn=jane.doe@example.com -e offboard_dry_run=false
ansible-playbook site.yml -e offboard_upn=jane.doe@example.com --tags okta,aws      # some systems only
ansible-playbook site.yml -e offboard_upn=jane.doe@example.com -e offboard_ticket=HR-1234
```

Tags: `okta` / `sso`, `entra`, `email`, `teams`, `azure_rbac`, `azure` (Entra ID and RBAC), and `aws`.

## The HR event

The rulebook expects a JSON `POST` like `examples/termination_event.json`:

```json
{ "event_type": "employee.terminated", "reference": "HR-2026-0412",
  "employee": { "upn": "jane.doe@example.com" } }
```

If your HR system sends a different shape, edit the `condition` and `extra_vars` in `rulebooks/offboarding.yml`. For example, Workday, BambooHR and ServiceNow each name these fields differently. Put the webhook behind TLS (a reverse proxy, or AAP's EDA event streams) whenever it is reachable from outside the host.

## Configuration

All settings live in `inventory/group_vars/all.yml` and are documented there. The main ones:

| Variable | Default | Purpose |
|---|---|---|
| `offboard_dry_run` | `true` | Report only; change nothing |
| `offboard_systems` | all `true` | Turn individual systems on or off |
| `protected_accounts` | break-glass example | Never offboard these accounts |
| `okta_action` | `deactivate` | Set to `suspend` for a reversible block |
| `entra_remove_licenses` | `false` | Removing the Exchange licence starts the 30-day mailbox deletion |
| `email_forward` | `false` | Forward new mail to the manager or to `email_forward_to` |
| `teams_fallback_owner` | `""` | New team owner when the user has no manager |
| `aws_accounts` | `[]` | `{name, profile}` for each AWS account |
| `aws_iam_username` | local part of UPN | IAM user name, if your naming differs |
| `azure_subscriptions` | `[]` | Subscriptions to check for role assignments |

## Not covered (do these by hand or add them)

- **Shared mailbox conversion, mailbox delegation and hiding the user from the GAL.** These need Exchange Online PowerShell and are not available in Graph.
- **OneDrive hand-over** to the manager (SharePoint admin).
- **PIM-eligible roles.** Only active directory roles are removed.
- **Hybrid AD.** For users synced from on-prem AD, disable the account in AD as well, or the next sync re-enables it in Entra ID.
- **Large accounts.** Graph group lists are capped at 999 entries per request, and there is no retry on throttling (HTTP 429).
