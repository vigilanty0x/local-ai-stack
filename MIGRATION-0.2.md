# Migration vers Local AI Stack 0.2.0

Local AI Stack 0.2.0 est une migration de qualité/release, pas une rupture d'API.

## Compatibilité

- Produit et dépôt : `Local AI Stack` / `vigilanty0x/local-ai-stack`.
- Distribution : `local-ai-stack`.
- Namespace Python : `local_ai_stack`.
- CLI : `local-ai-stack`.
- Contrat d'entrée historique conservé : objet JSON avec `runtime`, `model`, `status`.
- Codes de sortie conservés : `0` quand l'évidence passe, `2` quand elle échoue ou reste bloquée.

La 0.2 ajoute une surface `local-ai-stack --version`, des preuves de build et des gates de release, sans modifier le format de preuve `evidence_sha256` du moteur.

## Release gates

Avant toute publication 0.2, le SHA approuvé doit avoir :

1. CI Ubuntu/Windows/macOS sur Python 3.11-3.13 ;
2. wheel et sdist construits avec toolchain figée ;
3. tests depuis le wheel installé ;
4. preuve positive `status=ready`, contre-preuve `status=degraded`, et cas bloqué par champ manquant ;
5. smoke CLI hors checkout ;
6. sdist complet et auto-auditable ;
7. SHA-256 et SBOM CycloneDX 1.6 ;
8. provenance GitHub/Sigstore vérifiée ;
9. décision explicite de publication ;
10. vérification post-publication.

`release-policy.v1.json` garde `publish_enabled=false` dans cette migration.

## Rollback

Rollback vers 0.1.0. Aucune base de données, aucun secret et aucun état distant ne sont migrés. Le retour arrière est donc une réinstallation du wheel 0.1.0 vérifié ou un retour au commit/release correspondant.

## Repositories consolidés

`ollama-fleet-manager`, `local-model-benchmark` et `model-fallback-proxy` restent présents sous `packages/` avec leurs historiques. Cette migration ne les archive ni ne les supprime. Toute décision d'archive exige inventaire consommateurs, compatibilité/redirects, rollback et approbation humaine explicite.
