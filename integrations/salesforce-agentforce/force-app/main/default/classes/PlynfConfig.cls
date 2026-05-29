// SPDX-License-Identifier: Apache-2.0
/**
 * Loads the Plynf endpoint + API key from the Plynf_Config__mdt custom
 * metadata type. Centralised so individual flows / agent actions don't
 * each touch the metadata API.
 */
public with sharing class PlynfConfig {
    public final String endpoint;
    public final String apiKey;
    public final String model;

    public PlynfConfig(String endpoint, String apiKey, String model) {
        this.endpoint = endpoint;
        this.apiKey = apiKey;
        this.model = model;
    }

    /**
     * Read the configuration row named `Default` from custom metadata.
     * In dev orgs you can override by passing the name of a different
     * record into ``load('SandboxOverride')`` etc.
     */
    public static PlynfConfig load() {
        return load('Default');
    }

    public static PlynfConfig load(String developerName) {
        List<Plynf_Config__mdt> rows = [
            SELECT Endpoint__c, API_Key__c, Default_Model__c
            FROM Plynf_Config__mdt
            WHERE DeveloperName = :developerName
            LIMIT 1
        ];
        if (rows.isEmpty()) {
            throw new PlynfException(
                'Plynf_Config__mdt record "' + developerName +
                '" not found. Create it in Setup → Custom Metadata Types.'
            );
        }
        Plynf_Config__mdt r = rows[0];
        return new PlynfConfig(
            r.Endpoint__c,
            r.API_Key__c,
            String.isBlank(r.Default_Model__c) ? 'gpt-4o' : r.Default_Model__c
        );
    }

    public class PlynfException extends Exception {}
}
