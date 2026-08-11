# Privacy Policy

**Last Updated:** July 2026

**Service:** Wagerlabs - Chat Bot (Kick & Twitch), Discord Bot, Admin Dashboard & Browser Extension

## 1. Introduction

This Privacy Policy explains how we collect, use, store, and protect your personal information when you use the Kick Community Bot (the "Bot"), the Admin Dashboard, and the Wagerlabs Browser Extension (the "Extension") (collectively, the "Service"). By using the Service, you agree to the collection and use of information in accordance with this policy.

## 2. Information We Collect

### 2.1 Automatically Collected Information

**From Discord:**
- Discord User ID (unique identifier)
- Discord Username and display name
- Discord Server (Guild) ID
- Discord Role assignments and changes
- Message interaction timestamps
- Profile information for linked accounts

**From Kick.com:**
- Kick.com username
- Kick.com User ID
- Kick.com chat activity (username and timestamps only - message content not stored)
- Kick.com profile information accessed via OAuth
- Chat participation records

**Dashboard-Specific:**
- Dashboard login timestamps
- Administrative actions and modifications
- Settings changes and configuration updates
- Report generation and export requests
- Analytics queries and data access

### 2.2 Information You Provide

**Account Creation:**
You can create a Wagerlabs account in one of three ways, and we collect only what is needed for the method you choose:
- **Email & password:** your email address and a securely hashed password (we never store your password in plain text). We may send a verification email to confirm the address, and password-reset emails at your request.
- **Google Sign-In:** if you sign in with Google, we receive your Google account's email address, whether that email is verified, and your Google account identifier. We do not receive your Google password.
- **Discord:** if you sign in with Discord, we receive the Discord profile information described in Section 2.1.

**Account Linking:**
- OAuth authorization tokens (encrypted)
- Account linking timestamps
- Account linking/unlinking history

**Bot Usage:**
- Slot requests and selections
- Game entries (GTB, giveaways, raffles)
- Custom command definitions
- Gambling platform account information (for Shuffle, Stake, etc.)

**Dashboard Usage:**
- Configuration settings for bot behavior
- Role threshold settings
- Channel routing preferences
- Custom command creation
- Administrative notes and comments
- Feature toggle preferences

### 2.3 Generated Data

**Tracking & Statistics:**
- Watchtime statistics (minutes watched per period)
- Chat activity timestamps and frequency
- Leaderboard rankings and positions
- Role assignment history and dates

**Raffle System:**
- Raffle ticket balances (from watchtime, gifts, wagers)
- Raffle period participation records
- Raffle draw records and winner history
- Probability calculations and statistics

**Gambling Integration:**
- Multi-platform wager tracking (Shuffle, Stake, Stake.us, etc.)
- Wager amounts and conversion to tickets
- Account verification status
- Campaign code tracking (multiple codes per user)

**Other:**
- Gifted subscription records and ticket awards
- Game participation history (GTB, giveaways)
- Provably fair draw seeds and verification data
- Custom command usage statistics

### 2.4 Browser Extension

The Wagerlabs Browser Extension is an optional companion to your Admin Dashboard. It is installed in your own browser and works only with your own dashboard.

**What the Extension accesses:**
- **Dashboard session cookie:** the Extension reuses your existing wagerlabs.app login session to make authenticated requests to your dashboard's API. It does not read, store, or transmit cookies for any site other than your wagerlabs.app dashboard.
- **Local cache (in your browser only):** which dashboard server you are connected to, your extension preferences, and a short-lived copy of your active bonus hunt and bot status so the popup and overlay render instantly. This stays in the browser's local extension storage and is not sent to third parties.
- **Slot titles on casino pages:** on supported casino sites (Stake, Shuffle), the Extension reads the slot/game title from the page you are viewing so you can add that slot to your bonus hunt. It reads only the slot title and only on those sites; it does not read other page content, form fields, or your account details on those sites.

**What the Extension does NOT do:**
- It does not track your browsing history or activity across the web.
- It does not collect personal, financial, or payment information.
- It does not execute remote code; all of its code is contained in the published package.
- It only communicates with your own Wagerlabs dashboard — never with third-party servers.

### 2.5 Subscription and Billing Information

If you purchase a paid subscription, payment is processed by **Stripe, Inc.**, our third-party payment processor.

**We do NOT collect or store your full payment card details.** Your card number, expiry, and security code are entered directly into Stripe's systems and are handled by Stripe, not by us.

**What we store about a subscription** (in our database, keyed to your Discord server):
- A Stripe customer identifier and subscription identifier
- Your current subscription tier (free, tier2, or tier3) and status (active, past due, or canceled)
- The date your current billing period ends, and whether the subscription is set to cancel at period end

**What Stripe handles and may collect** to process your payment includes your payment method details, billing name, and related transaction information, in accordance with Stripe's own privacy policy (https://stripe.com/privacy). We receive from Stripe only the non-card information needed to provision and manage your subscription (such as the identifiers and status above).

## 3. How We Use Your Information

**Core Bot Functionality:**
- Link your Discord and Kick.com accounts via secure OAuth
- Track your watchtime based on chat participation
- Calculate raffle tickets from multiple sources (watchtime, gifted subs, wagers)
- Assign Discord roles based on configurable watchtime thresholds
- Display public leaderboards and personal statistics
- Process and route slot requests to Discord
- Manage raffle periods and draw winners
- Track gifted subscription events and award tickets
- Monitor gambling platform wagers and verify accounts
- Run interactive games (Guess The Balance)
- Execute custom commands and send timed messages

**Dashboard Functions:**
- Provide administrators with analytics and monitoring tools
- Enable management of bot settings and configurations
- Display real-time statistics and user activity
- Generate reports and export data
- Track administrative actions for audit purposes
- Manage role thresholds and feature configurations
- Control slot request management and blacklists
- Configure raffle periods and settings
- Verify gambling platform accounts
- Monitor bot health and status

**Browser Extension Functions:**
- Authenticate to your own dashboard using your existing login session
- Show your active bonus hunt, slot calls, Guess-the-Balance status, and raffle info
- Let you add slots to your hunt and trigger dashboard actions (pick random slot, open/close GTB, toggle slot requests) from supported sites
- Cache the above locally so the popup and in-page panel render instantly

**Billing & Subscriptions:**
- Provision and manage paid subscription tiers per Discord server
- Process recurring payments through Stripe and respond to payment events (renewal, cancellation, payment failure)
- Determine which features a server can access based on its current tier
- Provide access to the Stripe billing portal so you can manage your subscription and payment method

**Security & Compliance:**
- Prevent abuse, farming, and fraud through anti-farming measures
- Maintain service security and integrity
- Verify account ownership and prevent unauthorized access
- Provide audit trails and accountability
- Comply with administrator and server policies
- Implement rate limiting and abuse detection

**System Improvement:**
- Debug issues and improve functionality
- Analyze feature usage patterns
- Optimize performance and reliability
- Develop new features based on usage data
- Provide transparency and provably fair verification

**We DO NOT:**
- Sell personal information to third parties
- Share data for advertising or marketing
- Store chat message content (only usernames and timestamps)
- Track activity outside the Bot and Dashboard
- Use information for purposes unrelated to Bot/Dashboard operation
- Share information with unauthorized parties

## 4. Data Storage and Security

### 4.1 Data Storage

**Bot Data:**
- Stored in secure PostgreSQL database with SSL/TLS encryption
- Database connections fully encrypted in transit
- Access restricted to authenticated services only
- OAuth tokens encrypted at rest
- Automated backup systems with redundancy

**Dashboard Data:**
- Separate secure data store for administrative information
- Passwords for email accounts hashed with strong, salted algorithms (never stored in plain text)
- Session tokens encrypted and time-limited
- All dashboard actions logged and auditable
- Configuration changes encrypted and versioned

### 4.2 Data Retention

**Linked Accounts:**
- Retained until you unlink via `!unlink` or dashboard
- Can be deleted via account deletion request

**Watchtime Data:**
- Retained indefinitely unless deletion requested
- Kept for historical leaderboards and statistics

**OAuth Tokens:**
- Temporary tokens retained until expiration (1 hour for OAuth links)
- Refresh tokens retained until revoked
- Automatically cleared when accounts unlinked

**Raffle Data:**
- Retained indefinitely for transparency and auditability
- Draw records kept for provably fair verification
- Winner history maintained for statistics

**Gambling Platform Data:**
- Wager records retained indefinitely for platform compliance
- Verification status retained until account unlinked
- Campaign code tracking maintained for affiliate purposes

**Dashboard Data:**
- Administrative action logs retained for 90 days minimum
- Configuration history retained indefinitely
- Login records retained for security purposes

**Subscription & Billing Data:**
- Subscription records (tier, status, Stripe identifiers, period end) retained while the server exists and for a reasonable period afterward for accounting and tax purposes
- Full payment card details are never stored by us; they are held by Stripe under Stripe's retention practices

### 4.3 Security Measures

**Encryption & Authentication:**
- Database connections use SSL/TLS encryption
- OAuth tokens are encrypted at rest
- HMAC-SHA256 signature verification for OAuth links
- PKCE (Proof Key for Code Exchange) for OAuth security
- Time-limited OAuth state/token expiration
- Authentication via email & password, Google Sign-In, or Discord OAuth; dashboard access is scoped per workspace

**Access Control:**
- Environment-based credential management (no hardcoded secrets)
- Role-based access control for the dashboard
- Authentication required for all sensitive operations
- Admin actions logged with timestamp and user ID
- Rate limiting on sensitive endpoints

**Operational Security:**
- Payment webhooks are signature-verified before processing
- Dependencies are kept up to date
- Access is restricted to authenticated services

While we use industry-standard measures to protect your data, no method of transmission or storage is completely secure, and we cannot guarantee absolute security.

## 5. Data Sharing and Disclosure

### 5.1 When We Share Data

**With Your Consent (Public Data):**
- Leaderboard displays (Kick username and watchtime/tickets)
- Raffle/giveaway winner announcements
- Role assignments visible to Discord server members
- Raffle draw transparency and verification data
- Game participation announcements
- Public statistics and rankings

**Dashboard Access:**
- Server administrators can view all user data for their server
- Data is restricted to the specific server where it was generated
- Admins see complete audit trails of their own actions
- Multi-server setups keep data isolated per server

**Legal Requirements:**
- When required by law or legal process
- To protect rights, property, or safety of users
- To enforce Terms of Service
- To comply with valid court orders

**Service Providers:**
- **Discord** - For bot functionality, authentication, and message handling
- **Kick.com** and **Twitch** - For OAuth authentication and chat monitoring
- **Google** - For Google Sign-In (we receive your email address, its verified status, and Google account identifier)
- **Resend** - For sending transactional email (address verification, password resets, and support replies)
- **Stripe** - For processing paid subscription payments
- **Railway.app** - For hosting and infrastructure

### 5.2 What We DO NOT Share

We strictly do not:
- Sell your personal data to third parties
- Share data for marketing or advertising purposes
- Provide data to unauthorized parties
- Share OAuth tokens or refresh tokens
- Share gambling platform account credentials
- Share private account information
- Share Discord IDs or sensitive identifiers without authorization
- Share dashboard access credentials
- Share email addresses or contact information for marketing
- Use data for any purpose beyond stated functionality

This does not restrict the limited information necessarily provided to the service providers listed above (such as Stripe for payment processing) to operate the Service on your behalf.

## 6. Your Rights and Choices

### 6.1 Access and Control

You have the right to:
- **Access:** View your account information via Bot commands
- **Unlink:** Request to unlink your accounts via `!unlink` command
- **Delete:** Request deletion of your data
- **Correct:** Update information by relinking
- **Opt-Out:** Stop using the Bot at any time

### 6.2 How to Exercise Your Rights

To exercise your rights:
- **Unlink Account:** Use `!unlink` command
- **Delete Data:** Contact a server administrator
- **View Data:** Use `!watchtime`, `!tickets`, `!raffleboard` commands
- **Questions:** Contact server administrators

### 6.3 Data Deletion

If you request data deletion, we will remove:
- Your Discord-Kick account link
- Your watchtime statistics
- Your raffle ticket allocations
- Your OAuth tokens

Note: Raffle draw records may be retained for transparency.

## 7. Third-Party Services & Integrations

**Discord:**
- Privacy Policy: https://discord.com/privacy
- Used for: Bot API, message handling, user authentication, role management
- Compliance: We adhere to Discord's Terms of Service and Developer Policy

**Kick.com & Twitch:**
- Privacy Policy: https://kick.com/privacy and https://www.twitch.tv/p/legal/privacy-notice/
- Used for: OAuth authentication, chat monitoring, stream information
- Monitoring: We track public chat messages for watchtime; no private messages stored

**Google:**
- Privacy Policy: https://policies.google.com/privacy
- Used for: Google Sign-In (account creation and login)
- Data shared: We receive your email address, its verified status, and your Google account identifier; we do not receive your Google password

**Resend:**
- Privacy Policy: https://resend.com/legal/privacy-policy
- Used for: Sending transactional email — address verification, password resets, and support replies
- Data shared: The recipient email address and the contents of the message being sent

**Stripe:**
- Privacy Policy: https://stripe.com/privacy
- Used for: Processing paid subscription payments and recurring billing
- Data shared: Your payment method and billing details are provided directly to Stripe; we receive only subscription identifiers and status. We do not store full card details.

**Cloudflare Turnstile:**
- Privacy Policy: https://www.cloudflare.com/privacypolicy/
- Used for: An optional one-time bot check before entering a giveaway, when the server operator enables it
- Data shared: Your IP address and browser signals are sent to Cloudflare so it can score whether you are a real person. We do **not** store your IP address — we record only that your Discord account passed the check for that server, and when
- Retention: A passed check is remembered for 30 days per server, after which you would be asked again

**Gambling Platforms** (Shuffle, Stake, Stake.us, etc.):
- Used for: Wager tracking and verification via public APIs
- Data shared: Only your public account information and wager history
- Compliance: We follow each platform's API terms and privacy policies

**Hosting & Infrastructure:**
- **Railway.app** - Privacy Policy: https://railway.app/legal/privacy
- **PostgreSQL** - Open source database with community security practices
- **Amazon Web Services** (if applicable) - Privacy Policy: https://aws.amazon.com/privacy/

**Optional Services:**
- Real-time messaging (Redis/Pusher) for live updates
- Analytics and monitoring tools

**Disclaimer:** We are not responsible for the privacy practices of third-party services. Please review their privacy policies separately. When using integrations, you consent to sharing data with those platforms as required for functionality.

## 8. Children's Privacy

The Bot is not intended for users under the age of 13 (or the minimum age required in your jurisdiction). We do not knowingly collect personal information from children. If we become aware that a child has provided us with personal information, we will take steps to delete such information.

## 9. International Data Transfers

Your data may be transferred to and stored in countries outside your country of residence. By using the Bot, you consent to such transfers. We ensure appropriate safeguards are in place to protect your data.

## 10. Cookies and Tracking

- We use a session cookie to keep you signed in, whether you authenticate with email & password, Google, or Discord
- We do not use tracking cookies or third-party analytics
- OAuth flow uses temporary state tokens for security
- The Browser Extension reuses your existing dashboard session cookie to authenticate API requests to your own dashboard; it does not set tracking cookies and does not read cookies for any other site

## 11. Changes to This Privacy Policy

We may update this Privacy Policy from time to time. Changes will be posted with an updated "Last Updated" date. Your continued use of the Bot after changes constitutes acceptance of the updated Privacy Policy.

## 12. Data Breach Notification

In the unlikely event of a data breach affecting your personal information, we will:
- Investigate the breach promptly
- Notify affected users as soon as reasonably possible
- Take steps to mitigate harm
- Report to relevant authorities if required by law

## 13. Compliance

We strive to comply with applicable data protection laws, including:

### GDPR Rights (EU Users)

If you are in the European Union, you have additional rights:
- Right to access your data
- Right to rectification of inaccurate data
- Right to erasure ("right to be forgotten")
- Right to restrict processing
- Right to data portability
- Right to object to processing
- Right to withdraw consent

### CCPA Rights (California Users)

If you are a California resident, you have the right to:
- Know what personal information is collected
- Request deletion of your personal information
- Non-discrimination for exercising your rights

## 14. Data Minimization

We practice data minimization by:
- Collecting only necessary information for Bot functionality
- Not storing chat message content (only usernames and timestamps)
- Automatically deleting expired OAuth states
- Limiting data retention to what is necessary

## 15. Automated Decision Making

The Bot uses automated processing for:
- Watchtime calculation based on chat activity
- Automatic role assignment based on thresholds
- Raffle ticket allocation
- Winner selection for raffles
- Anti-farming detection

These automated decisions do not have legal effects beyond Discord server role assignments and optional reward systems.

## 16. Contact Information

For privacy-related questions, concerns, billing inquiries, or data requests:
- Email **support@wagerlabs.app**
- For requests specific to a particular Discord community, you may also contact that server's administrators (for data deletion, an administrator with "Manage Server" permissions)
- Visit the Admin Dashboard for server-specific support

---

**By using the Service, you acknowledge that you have read and understood this Privacy Policy.**
