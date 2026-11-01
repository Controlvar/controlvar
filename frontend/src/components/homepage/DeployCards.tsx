import React from 'react';
import { docsUrl } from '@/services/docs';
import { COMPOSE_GUIDE_URL } from '@/content/site-links';
import { Quote } from '@/components/homepage/Quote';
import { FOCUS, WIDTH } from '@/components/homepage/styles';

const LINK = `font-semibold text-blue-700 hover:text-blue-900 ${FOCUS}`;

function Card({
  title,
  icon,
  children,
  link,
}: {
  title: string;
  icon: string[];
  children: React.ReactNode;
  link: { text: string; href: string };
}) {
  return (
    <li className="flex min-w-0 flex-col rounded-xl border border-slate-200 p-6">
      <svg className="h-6 w-6 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        {icon.map((d) => (
          <path key={d} d={d} />
        ))}
      </svg>
      <h3 className="mt-3 text-lg font-semibold">{title}</h3>
      {children}
      <p className="mt-auto pt-4">
        <a href={link.href} className={LINK}>
          {link.text} <span aria-hidden="true">→</span>
        </a>
      </p>
    </li>
  );
}

/**
 * Three ways to deploy it. The Compose card links the self-hosting stack's
 * README, which is under deploy/, not docs/ (`content/site-links.ts`); the
 * other two are docs pages, through literal `docsUrl` calls. The AWS card
 * carries the installer beta's sentence from `content/home-quotes.json`, as a
 * sentence of that card and not a fourth card: a beta "not for production
 * data" is not a peer of the other three.
 */
export function DeployCards() {
  return (
    <section aria-labelledby="deploy-heading" className={`${WIDTH} py-16`}>
      <h2 id="deploy-heading" className="text-3xl font-semibold tracking-tight">
        Deploy it yourself
      </h2>
      <ul role="list" className="mt-8 grid gap-5 lg:grid-cols-3">
        <Card
          title="Docker Compose"
          icon={['M5 4h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z', 'M8 20h8M12 16v4']}
          link={{ text: 'Docker Compose (self-hosting stack)', href: COMPOSE_GUIDE_URL }}
        >
          <p className="mt-2 leading-relaxed text-slate-600">Run the published container images on one machine.</p>
        </Card>
        <Card
          title="Kubernetes"
          icon={['M12 2l9 5v10l-9 5-9-5V7z', 'M12 22V12M21 7l-9 5-9-5']}
          link={{ text: 'Self-host on Kubernetes', href: docsUrl('self-hosting/kubernetes') }}
        >
          <p className="mt-2 leading-relaxed text-slate-600">Install them with the Helm chart attached to each release.</p>
        </Card>
        <Card
          title="Your AWS account"
          icon={['M7 18a4.5 4.5 0 0 1-.5-9A6 6 0 0 1 18 8a4 4 0 0 1-.5 10z']}
          link={{ text: 'Deploy with CDK', href: docsUrl('self-hosting/cdk') }}
        >
          <p className="mt-2 leading-relaxed text-slate-600">
            Use the CDK app to deploy to ECS Fargate, Aurora PostgreSQL and ElastiCache in your own AWS account.
          </p>
          <p className="mt-2 text-sm leading-relaxed text-slate-600">
            <Quote id="aws-installer" />
          </p>
        </Card>
      </ul>
    </section>
  );
}
