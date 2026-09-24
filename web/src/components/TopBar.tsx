import Link from "next/link";
import styles from "./TopBar.module.css";

/** Slim shared top bar across the live call page and the session review
 * UI. Server component: no interactivity of its own. */
export default function TopBar() {
  return (
    <header className={styles.bar}>
      <span className={styles.brand}>
        TurnTrace <span className={styles.subtitle}>· Voice Agent Session Inspector</span>
      </span>
      <nav className={styles.nav} aria-label="Primary">
        <Link href="/" className={styles.link}>
          Live
        </Link>
        <Link href="/sessions" className={styles.link}>
          Sessions
        </Link>
      </nav>
    </header>
  );
}
