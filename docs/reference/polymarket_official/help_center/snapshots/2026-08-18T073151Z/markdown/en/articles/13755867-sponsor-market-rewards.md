# Sponsor Market Rewards

When you sponsor a market, you deposit USDC into a smart contract that automatically distributes rewards to liquidity providers on that market. The more liquidity someone provides, the larger their share of your sponsored rewards.

- **Choose a market**: Pick any market you want to see more liquidity on.

- **Set your budget and duration**: Deposit USDC and choose how long you want to sponsor rewards (e.g., $500 over 10 days = $50/day in rewards).

- **Rewards are distributed daily**: Liquidity providers earn their share of your sponsored rewards once every 24 hours, at 12:00 AM UTC.

- **Auto-refund on early resolution:** If the market resolves before your sponsorship ends, any unspent funds are automatically returned to your wallet.

## Sponsoring a Market

1. Navigate to the **[Daily Rewards](https://polymarket.com/rewards)** page.

2. Find the market you want to sponsor and click **Add**, or click **Rewards** on the market page.

3. Enter the amount you'd like to commit (Minimum: $0.1 per day).

4. Use the duration buttons to set how many days you want the sponsorship to last.

5. Review the daily reward rate and commitment end date.

6. Confirm the transaction.

![](https://downloads.intercomcdn.com/i/o/zryw7npl/2075895515/534e66190097bf0907f83b14e41b/image.png?expires=1787040000&amp;signature=a29aca49027fdd05ce26f74cc73788869baeb8a51947867c1fe9741068e62835&amp;req=diAgE8F3mIReXPMW1HO4ze%2FO3a7IFdy2Gsc6ULLN62DV9eIwJz%2BEl9J0Yr5u%0AYj1YnpfXqJ%2FWwPL9pS4%3D%0A)

Your USDC is deposited into the Rewards smart contract and begins distributing to liquidity providers at the next 12:00 AM UTC cycle.

You can only have one active sponsorship per market at a time. Once a sponsorship ends or is cancelled, you can sponsor the same market again with a different amount or duration.

View all your active and past sponsorships in **Portfolio → Sponsorships**.

## Cancelling a sponsorship

You can cancel an active sponsorship at any time. Your refund is processed immediately, **but it only covers the period from the next 12:00 AM UTC onward**.

Here's how it works: if you cancel at, say, 4:00 PM UTC, the rewards between now and midnight stay committed to the reward pool for liquidity providers. Your refund covers everything from midnight onward. Any portion of today's rewards that isn't earned by liquidity providers is also returned to your wallet automatically at 12:00 AM UTC.

**This means you won't get the full deposited amount back, even if you cancel shortly after confirming.** The gap depends on how close to midnight you cancel and how much liquidity providers earn from today's pool.
​

**To cancel:**

1. Go to **Portfolio → Sponsorships**.

2. Click on the **Cancel** button next to the active sponsorship you want to cancel.

3. Review the refund breakdown showing your committed amount, amount spent, and available refund.

4. Confirm the cancellation.

The refund is sent to your wallet automatically.

![](https://downloads.intercomcdn.com/i/o/zryw7npl/2080397482/c306f3ade1ed09667e649dd5ae35/image.png?expires=1787040000&amp;signature=39d977aaa4dea0b31f3e1b885064797e6cd00a9f5f995859132219b93e0dd32f&amp;req=diAvFsp3moVXW%2FMW1HO4zejCYv%2B5WItJYzLWR2USXcOUhFl2XAutmihr3eIH%0Aetcp%2BkxE%2Bi1JYyKqRaY%3D%0A)

## Auto-Refunds

If a market resolves before your sponsorship period ends, you don't lose the remaining funds. The smart contract calculates the amount used and returns the rest to your wallet automatically.

**Example**: You sponsor $500 over 10 days. The market resolves after 5 days. $250 was distributed to liquidity providers; the remaining $250 is returned to your wallet.

## FAQ

1. **Do I receive any monetary benefit as the Sponsor?** There are no rewards for the Sponsor providing funds to the Rewards Pool. The primary incentive to sponsor rewards is to drive liquidity into that market.

2. **Can I cancel a sponsorship early?** Yes. You can cancel anytime from Portfolio → Sponsorships. Your refund covers the remaining balance from the next 12:00 AM UTC onward. Today's allocated rewards stay in the pool for liquidity providers, and any portion they don't earn is returned to you shortly after 12:00 AM UTC.

3. **Can I top up or change the amount on an active sponsorship?** No. Each sponsorship is a fixed commitment. If you want to sponsor with a different amount, cancel or wait for the current one to end, then create a new sponsorship.

4. **How much of my sponsored rewards are distributed daily?** Your daily rate (e.g. $50/day) is the maximum that can be distributed each cycle. Liquidity providers earn from that pool based on their share of market liquidity. If the pool isn't fully earned, for example, if there's low liquidity activity on a given day, the unused daily portion is returned to you at the end of that day. You never spend more than your daily rate.

5. **Can I sponsor the same market again?** Yes, once your current sponsorship on that market has ended or been cancelled. You can set a different amount and duration each time.

6. **What happens if multiple people sponsor the same market?** All sponsorship rewards stack. Liquidity providers earn from the combined total of all active sponsorships plus any native Polymarket rewards.

7. **Where do my funds go?** Your USDC is held in the Rewards smart contract onchain. It is distributed to liquidity providers once every 24 hours at 12:00 AM UTC, proportional to their share of market liquidity.

8. **How are rewards calculated for liquidity providers?** Rewards are distributed proportionally to each provider's share of market liquidity, using the same scoring formula as native Polymarket rewards.

9. **Why can't I get a full refund when I cancel?** Sponsorships run on a 24-hour cycle. When you cancel, today's rewards have already been allocated to the pool and can't be pulled back mid-cycle. This prevents someone from briefly sponsoring a market to attract liquidity and withdrawing before any rewards are actually paid out. Any portion of today's allocation that liquidity providers don't earn is still returned to you at the end of the day (12 AM UTC)

10. **Why does this use a smart contract?** Polymarket never takes custody of your funds. Your USDC is held in an onchain contract that only your wallet can interact with. you control the deposit, cancellation, and any refunds. No one else can withdraw or redirect your sponsored funds.